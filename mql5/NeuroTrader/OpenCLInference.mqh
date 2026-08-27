//+------------------------------------------------------------------+
//| OpenCLInference.mqh - GPU inference inside the EA                |
//|                                                                  |
//| Part of NeuroTrader (TZ_BOOKS task T-18; NN book chapters 5-6,   |
//| pages 995-1335).                                                 |
//|                                                                  |
//| The book's key OpenCL lesson (5.1.5 + 5.4): context/queue/       |
//| program creation takes ~seconds of wall time. The naive          |
//| implementation rebuilt the context on every inference and the    |
//| resulting latency made the whole approach pointless. The fix     |
//| (5.4, opencl_inference.mqh): create context ONCE in OnInit,      |
//| reuse buffers, only CLKernelCreate/CLExecute per bar.            |
//|                                                                  |
//| This module runs a small MLP forward pass (the FC stack trained  |
//| by scripts/run_book_experiments.py, exported as weights) on the  |
//| GPU; when no OpenCL device is available it silently falls back   |
//| to an identical CPU implementation in MQL5 (book 5.4.4: OpenCL   |
//| is an accelerator, not a dependency).                            |
//|                                                                  |
//| Kernel: one work-item per sample; hidden layer Swish, output     |
//| sigmoid - mirrors model/book_nn (layers.py).                     |
//+------------------------------------------------------------------+
#ifndef NEUROTRADER_OPENCL_INFERENCE_MQH
#define NEUROTRADER_OPENCL_INFERENCE_MQH

#define OCL_HIDDEN_MAX 256

class COpenCLInference
{
private:
   int    m_context;         // OpenCL context handle
   int    m_queue;
   int    m_program;
   int    m_kernel;
   int    m_inputBuf;        // input features buffer
   int    m_weightBuf;       // W1 (in*h) | b1 (h) | W2 (h) | b2 (1)
   int    m_outputBuf;       // output probability
   int    m_inputDim;
   int    m_hiddenDim;
   bool   m_useOpenCL;       // false = CPU fallback active
   bool   m_ready;

   //--- CPU fallback weights (same layout as the GPU buffer)
   double m_w1[];
   double m_b1[];
   double m_w2[];
   double m_b2[];

   string KernelSource() const
   {
      string src =
         "__kernel void mlp_forward(const __global double *features,   \n"
         "                         const __global double *weights,     \n"
         "                         __global double *out)               \n"
         "{                                                               \n"
         "   const int i = get_global_id(0);                             \n"
         "   const int IN = " + (string)m_inputDim + ";                 \n"
         "   const int H  = " + (string)m_hiddenDim + ";                \n"
         "   const __global double *w1 = weights;                       \n"
         "   const __global double *b1 = weights + IN * H;              \n"
         "   const __global double *w2 = weights + IN * H + H;          \n"
         "   const __global double *b2 = weights + IN * H + H + H;      \n"
         "   const __global double *x  = features + i * IN;             \n"
         "   double z = 0.0;                                            \n"
         "   for(int h = 0; h < H; h++)                                 \n"
         "   {                                                           \n"
         "      double a = b1[h];                                       \n"
         "      for(int k = 0; k < IN; k++)                             \n"
         "         a += x[k] * w1[k * H + h];                           \n"
         "      // Swish: x * sigmoid(x)  (model/book_nn layers.py)      \n"
         "      double s = a / (1.0 + exp(-a));                         \n"
         "      z += s * w2[h];                                         \n"
         "   }                                                           \n"
         "   z += b2[0];                                                \n"
         "   out[i] = 1.0 / (1.0 + exp(-z));   // sigmoid probability    \n"
         "}                                                              \n";
      return src;
   }

public:
   COpenCLInference()
   {
      m_context  = -1;
      m_queue    = -1;
      m_program  = -1;
      m_kernel   = -1;
      m_inputBuf = -1;
      m_weightBuf= -1;
      m_outputBuf= -1;
      m_inputDim = 0;
      m_hiddenDim= 0;
      m_useOpenCL= false;
      m_ready    = false;
   }

   ~COpenCLInference()
   {
      if(m_kernel    != -1) CLKernelFree(m_kernel);
      if(m_program   != -1) CLProgramFree(m_program);
      if(m_queue     != -1) CLQueueFree(m_queue);
      if(m_context   != -1) CLContextFree(m_context);
      if(m_inputBuf  != -1) CLBufferFree(m_inputBuf);
      if(m_weightBuf != -1) CLBufferFree(m_weightBuf);
      if(m_outputBuf != -1) CLBufferFree(m_outputBuf);
   }

   bool UsesOpenCL() const { return m_useOpenCL; }
   bool IsReady()    const { return m_ready; }

   //+--------------------------------------------------------------+
   //| One-time initialization (book 5.4): context + queue + program |
   //| are created ONCE here, never in the inference path.           |
   //| weights layout: w1[in*h], b1[h], w2[h], b2[1] (row-major).    |
   //+--------------------------------------------------------------+
   bool Init(const int inputDim, const int hiddenDim,
             const double &weights[])
   {
      m_inputDim  = inputDim;
      m_hiddenDim = MathMin(hiddenDim, OCL_HIDDEN_MAX);
      m_ready     = false;

      int expected = inputDim * m_hiddenDim + m_hiddenDim + m_hiddenDim + 1;
      if(ArraySize(weights) < expected)
      {
         PrintFormat("[OpenCLInference] weight buffer too small: %d < %d",
                     ArraySize(weights), expected);
         return false;
      }

      //--- CPU fallback always loaded first (book 5.4.4: OpenCL is an
      //    accelerator, not a dependency)
      ArrayResize(m_w1, inputDim * m_hiddenDim);
      ArrayResize(m_b1, m_hiddenDim);
      ArrayResize(m_w2, m_hiddenDim);
      ArrayResize(m_b2, 1);
      ArrayCopy(m_w1, weights, 0, 0, inputDim * m_hiddenDim);
      ArrayCopy(m_b1, weights, 0, inputDim * m_hiddenDim, m_hiddenDim);
      ArrayCopy(m_w2, weights, 0, inputDim * m_hiddenDim + m_hiddenDim, m_hiddenDim);
      ArrayCopy(m_b2, weights, 0, inputDim * m_hiddenDim + 2 * m_hiddenDim, 1);
      m_ready = true;

      //--- try OpenCL
      ResetLastError();
      m_context = CLContextCreate();
      if(m_context < 0)
      {
         Print("[OpenCLInference] no OpenCL device (err ", GetLastError(),
               ") - using CPU fallback");
         m_useOpenCL = false;
         return true;                       // ready on CPU
      }
      m_queue = CLQueueCreate(m_context);
      if(m_queue < 0)
      {
         CLContextFree(m_context);
         m_context = -1;
         m_useOpenCL = false;
         return true;
      }

      m_program = CLProgramCreate(m_context, KernelSource());
      if(m_program < 0)
      {
         Print("[OpenCLInference] program build failed (err ", GetLastError(),
               ") - using CPU fallback");
         CLQueueFree(m_queue);
         m_queue = -1;
         CLContextFree(m_context);
         m_context = -1;
         return true;
      }
      m_kernel = CLKernelCreate(m_program, "mlp_forward");
      if(m_kernel < 0)
      {
         CLProgramFree(m_program);
         m_program = -1;
         CLQueueFree(m_queue);
         m_queue = -1;
         CLContextFree(m_context);
         m_context = -1;
         return true;
      }

      //--- persistent buffers (allocated once - book 5.4)
      m_inputBuf  = CLBufferCreate(m_context, inputDim * sizeof(double),
                                    CL_MEM_READ_ONLY);
      m_weightBuf = CLBufferCreate(m_context, expected * sizeof(double),
                                    CL_MEM_READ_ONLY);
      m_outputBuf = CLBufferCreate(m_context, sizeof(double), CL_MEM_WRITE_ONLY);
      if(m_inputBuf < 0 || m_weightBuf < 0 || m_outputBuf < 0)
      {
         Print("[OpenCLInference] buffer allocation failed - CPU fallback");
         if(m_inputBuf  != -1) CLBufferFree(m_inputBuf);
         if(m_weightBuf != -1) CLBufferFree(m_weightBuf);
         if(m_outputBuf != -1) CLBufferFree(m_outputBuf);
         m_inputBuf = m_weightBuf = m_outputBuf = -1;
         CLKernelFree(m_kernel);
         m_kernel = -1;
         CLProgramFree(m_program);
         m_program = -1;
         CLQueueFree(m_queue);
         m_queue = -1;
         CLContextFree(m_context);
         m_context = -1;
         return true;
      }

      //--- upload weights once
      if(!CLBufferWrite(m_queue, m_weightBuf, weights, 0, 0, expected))
      {
         Print("[OpenCLInference] weight upload failed - CPU fallback");
         m_useOpenCL = false;
         return true;
      }

      CLSetKernelArgMem(m_kernel, 0, m_inputBuf);
      CLSetKernelArgMem(m_kernel, 1, m_weightBuf);
      CLSetKernelArgMem(m_kernel, 2, m_outputBuf);

      m_useOpenCL = true;
      PrintFormat("[OpenCLInference] GPU path active: %d->%d->1",
                  m_inputDim, m_hiddenDim);
      return true;
   }

   //+--------------------------------------------------------------+
   //| Single-sample inference (one bar's features -> probability).   |
   //| Returns false only on a hard failure.                          |
   //+--------------------------------------------------------------+
   bool Forward(const double &features[], double &probability)
   {
      if(!m_ready || ArraySize(features) < m_inputDim)
         return false;

      if(m_useOpenCL)
      {
         if(!CLBufferWrite(m_queue, m_inputBuf, features, 0, 0, m_inputDim))
         {
            Print("[OpenCLInference] input upload failed - switching to CPU");
            m_useOpenCL = false;
         }
         else
         {
            uint offsets[1] = {0};
            uint globals[1] = {1};
            if(!CLExecute(m_kernel, 1, offsets, globals))
            {
               Print("[OpenCLInference] execution failed - switching to CPU");
               m_useOpenCL = false;
            }
            else
            {
               double out[1];
               if(!CLBufferRead(m_queue, m_outputBuf, out, 0, 0, 1))
               {
                  m_useOpenCL = false;
               }
               else
               {
                  probability = out[0];
                  return true;
               }
            }
         }
      }

      //--- CPU fallback: identical math to the kernel
      double z = 0.0;
      for(int h = 0; h < m_hiddenDim; h++)
      {
         double a = m_b1[h];
         for(int k = 0; k < m_inputDim; k++)
            a += features[k] * m_w1[k * m_hiddenDim + h];
         double s = a / (1.0 + MathExp(-a));    // Swish
         z += s * m_w2[h];
      }
      z += m_b2[0];
      probability = 1.0 / (1.0 + MathExp(-z));
      return true;
   }

   //+--------------------------------------------------------------+
   //| Weight export helper: flat [w1 | b1 | w2 | b2] from Python's   |
   //| book_fc.npz (W1 (in,h), b1 (h,), W2 (h,), b2 (1,)).           |
   //+--------------------------------------------------------------+
   static int FlattenWeights(const double &w1[], const double &b1[],
                             const double &w2[], const double &b2[],
                             double &flat[])
   {
      int in_h = ArraySize(w1);
      int h    = ArraySize(b1);
      int total = in_h + h + ArraySize(w2) + ArraySize(b2);
      ArrayResize(flat, total);
      int pos = 0;
      for(int i = 0; i < in_h; i++) flat[pos++] = w1[i];
      for(int i = 0; i < h;  i++)  flat[pos++] = b1[i];
      for(int i = 0; i < ArraySize(w2); i++) flat[pos++] = w2[i];
      for(int i = 0; i < ArraySize(b2); i++) flat[pos++] = b2[i];
      return total;
   }
};

#endif // NEUROTRADER_OPENCL_INFERENCE_MQH

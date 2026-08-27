//+------------------------------------------------------------------+
//| BenchmarkOpenCL.mq5 - GPU vs CPU inference latency (T-18)        |
//|                                                                  |
//| Part of NeuroTrader (TZ_BOOKS task T-18; NN book 3.7+ + 5.4).    |
//|                                                                  |
//| Measures the ACTUAL per-inference latency of the shared          |
//| COpenCLInference kernel against its CPU fallback, the way the   |
//| book benchmarks OpenCL in 5.4: N forward passes of one feature   |
//| vector, wall-clock, repeated and reported per mode.              |
//|                                                                  |
//| The book's headline result (5.4): the FIRST OpenCL call pays     |
//| seconds of device initialization, so context creation is hoisted |
//| out of the measured loop here exactly as it is in the EA.        |
//|                                                                  |
//| Run as a Script on any chart; results go to the Experts log and  |
//| to MQL5\Files\opencl_benchmark.csv.                              |
//+------------------------------------------------------------------+
#property copyright "xauusd-alert-system / books integration"
#property version   "1.00"
#property script_show_inputs
#property strict

#include "../OpenCLInference.mqh"

input int    InpInputDim      = 7;      // feature count (book feature set)
input int    InpHiddenDim     = 60;     // FC hidden width
input int    InpPasses        = 20000;  // forward passes per mode
input int    InpWarmup        = 200;    // unmeasured warm-up passes
input int    InpSeed          = 42;

//+------------------------------------------------------------------+
void OnStart()
{
   MathSrand(InpSeed);

   //--- random weights and one feature vector
   int weightCount = InpInputDim * InpHiddenDim + InpHiddenDim
                   + InpHiddenDim + 1;
   double weights[];
   ArrayResize(weights, weightCount);
   for(int i = 0; i < weightCount; i++)
      weights[i] = MathRand() / 32767.0 - 0.5;

   double features[];
   ArrayResize(features, InpInputDim);
   for(int i = 0; i < InpInputDim; i++)
      features[i] = MathRand() / 32767.0 - 0.5;

   COpenCLInference inference;
   if(!inference.Init(InpInputDim, InpHiddenDim, weights))
   {
      Print("[BenchmarkOpenCL] init failed");
      return;
   }
   bool gpu = inference.UsesOpenCL();

   //--- sanity: both paths must agree on the output
   double p = 0.0;
   inference.Forward(features, p);
   PrintFormat("[BenchmarkOpenCL] sample probability: %.6f (%s path)",
               p, gpu ? "GPU" : "CPU");

   //--- warm-up (device init, caches - excluded from the measurement)
   double probe = 0.0;
   for(int i = 0; i < InpWarmup; i++)
      inference.Forward(features, probe);

   //--- measured GPU/CUDA path
   uint started = GetTickCount();
   double checksum = 0.0;
   for(int i = 0; i < InpPasses; i++)
   {
      features[0] = (double)i / InpPasses;    // vary input, defeat caches
      if(inference.Forward(features, probe))
         checksum += probe;
   }
   uint elapsedOpenCL = GetTickCount() - started;

   //--- CPU reference path: same math, deliberately in the script so the
   //    comparison is self-contained (identical formula as the kernel)
   started = GetTickCount();
   double checksumCpu = 0.0;
   for(int i = 0; i < InpPasses; i++)
   {
      features[0] = (double)i / InpPasses;
      double z = 0.0;
      for(int h = 0; h < InpHiddenDim; h++)
      {
         double a = weights[InpInputDim * InpHiddenDim + h];
         for(int k = 0; k < InpInputDim; k++)
            a += features[k]
               * weights[k * InpHiddenDim + h];
         double s = a / (1.0 + MathExp(-a));
         z += s * weights[InpInputDim * InpHiddenDim + InpHiddenDim + h];
      }
      z += weights[weightCount - 1];
      checksumCpu += 1.0 / (1.0 + MathExp(-z));
   }
   uint elapsedCpu = GetTickCount() - started;

   //--- report
   double perCallOpenCL = (double)elapsedOpenCL * 1000.0 / InpPasses;  // us
   double perCallCpu    = (double)elapsedCpu    * 1000.0 / InpPasses;
   double speedup       = (elapsedOpenCL > 0 && elapsedCpu > 0)
                          ? (double)elapsedCpu / (double)elapsedOpenCL : 0.0;

   PrintFormat("[BenchmarkOpenCL] %s: %d passes in %u ms (%.1f us/call)",
               gpu ? "OpenCL" : "OpenCL-unavailable-CPU",
               InpPasses, elapsedOpenCL, perCallOpenCL);
   PrintFormat("[BenchmarkOpenCL] CPU reference: %d passes in %u ms "
               "(%.1f us/call)", InpPasses, elapsedCpu, perCallCpu);
   PrintFormat("[BenchmarkOpenCL] checksums: cl=%.6f cpu=%.6f (must match)",
               checksum, checksumCpu);
   if(gpu)
      PrintFormat("[BenchmarkOpenCL] speedup: %.2fx", speedup);
   else
      Print("[BenchmarkOpenCL] no OpenCL device: benchmark degenerates to "
            "CPU-vs-CPU (book 5.4.4 - OpenCL stays optional)");

   int file = FileOpen("opencl_benchmark.csv",
                       FILE_WRITE | FILE_CSV | FILE_ANSI, ';');
   if(file != INVALID_HANDLE)
   {
      FileWrite(file, "metric", "value");
      FileWrite(file, "device", gpu ? "OpenCL" : "CPU-fallback");
      FileWrite(file, "input_dim", InpInputDim);
      FileWrite(file, "hidden_dim", InpHiddenDim);
      FileWrite(file, "passes", InpPasses);
      FileWrite(file, "opencl_ms", elapsedOpenCL);
      FileWrite(file, "cpu_ms", elapsedCpu);
      FileWrite(file, "opencl_us_per_call", perCallOpenCL);
      FileWrite(file, "cpu_us_per_call", perCallCpu);
      FileWrite(file, "speedup", speedup);
      FileWrite(file, "checksum_cl", checksum);
      FileWrite(file, "checksum_cpu", checksumCpu);
      FileClose(file);
      Print("[BenchmarkOpenCL] written MQL5\\Files\\opencl_benchmark.csv");
   }
}
//+------------------------------------------------------------------+

import React from 'react';
import { Send, X, Copy, Check, MessageSquare } from 'lucide-react';
import { SignalResponse } from '../types';

interface TelegramPreviewModalProps {
  isOpen: boolean;
  onClose: () => void;
  signal: SignalResponse | null;
}

export const TelegramPreviewModal: React.FC<TelegramPreviewModalProps> = ({
  isOpen,
  onClose,
  signal,
}) => {
  const [copied, setCopied] = React.useState(false);

  if (!isOpen) return null;

  const sampleSignalText = signal ? `🟡 <b>INSTITUTIONAL SIGNAL ALERT | ${signal.asset}</b>
━━━━━━━━━━━━━━━━━━━━
⚡ <b>Action:</b> ${signal.bias.toUpperCase()}
🎯 <b>Timeframe:</b> ${signal.setup_timeframe} (Context: H1 / H4)
📊 <b>Regime:</b> ${signal.regime.toUpperCase()}
🔥 <b>ML Confidence:</b> ${(signal.confidence * 100).toFixed(1)}%

📍 <b>Entry Zone:</b> ${signal.entry_zone ? signal.entry_zone.join(' – ') : signal.targets?.[0]}
🛑 <b>Stop Loss:</b> ${signal.invalidation}
🎯 <b>TP 1:</b> ${signal.targets?.[0]} (33% + Breakeven Trigger)
🎯 <b>TP 2:</b> ${signal.targets?.[1]} (33% Size Reduction)
🎯 <b>TP 3:</b> ${signal.targets?.[2]} (Runner / Final Exit)

💡 <b>SMC Rationale:</b>
${signal.reasoning_summary}

🔐 <b>Spec Hash:</b> <code>${signal.strategy_spec_hash?.slice(0, 12)}</code>
⏱ <b>Timestamp:</b> ${signal.generated_at}
━━━━━━━━━━━━━━━━━━━━
<i>*Trade strictly with 0.25% - 0.50% account risk. Automated Breakeven armed.</i>` : '';

  const handleCopy = () => {
    navigator.clipboard.writeText(sampleSignalText);
    setCopied(true);
    setTimeout(() => setCopied(false), 2000);
  };

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center p-4 bg-slate-950/80 backdrop-blur-sm animate-in fade-in duration-200">
      <div className="bg-slate-900 border border-slate-700 rounded-xl max-w-lg w-full p-6 shadow-2xl relative">
        <div className="flex justify-between items-center pb-4 border-b border-slate-800">
          <div className="flex items-center gap-2">
            <div className="p-1.5 rounded bg-sky-500/10 text-sky-400 border border-sky-500/20">
              <Send className="w-4 h-4" />
            </div>
            <div>
              <h3 className="text-base font-bold text-slate-100">
                Telegram Dispatch Alert Formatter
              </h3>
              <p className="text-xs text-slate-400">
                Real-time subscriber format simulation
              </p>
            </div>
          </div>
          <button
            onClick={onClose}
            className="text-slate-400 hover:text-slate-200 p-1.5 rounded-lg hover:bg-slate-800 transition-colors"
          >
            <X className="w-5 h-5" />
          </button>
        </div>

        {/* Telegram Chat Simulation Bubble */}
        <div className="mt-4 bg-[#17212b] p-4 rounded-xl border border-sky-900/30 text-slate-200 text-xs font-mono whitespace-pre-wrap leading-relaxed shadow-inner">
          <div dangerouslySetInnerHTML={{ __html: sampleSignalText }} />
        </div>

        {/* Action footer */}
        <div className="mt-4 flex justify-between items-center">
          <span className="text-[11px] text-slate-500 font-mono">
            Bot: @xauusd_alert_bot
          </span>
          <button
            onClick={handleCopy}
            className="flex items-center gap-1.5 bg-sky-600 hover:bg-sky-500 text-white text-xs px-3.5 py-2 rounded-lg font-medium transition-colors"
          >
            {copied ? <Check className="w-3.5 h-3.5" /> : <Copy className="w-3.5 h-3.5" />}
            <span>{copied ? 'Copied' : 'Copy Telegram Message'}</span>
          </button>
        </div>
      </div>
    </div>
  );
};

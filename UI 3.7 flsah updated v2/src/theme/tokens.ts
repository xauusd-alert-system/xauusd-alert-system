/**
 * Design Tokens and Status Display Contract.
 * Invariant: Never use color as the sole indicator of state.
 * Always pair Color + Icon + Explicit Text.
 */

export interface StatusBadge {
  label: string;
  badgeClass: string;
  dotColor: string;
  icon: string;
}

export function getFreshnessBadge(
  freshness: string | undefined | null,
  source?: string,
  mode?: string
): StatusBadge {
  switch (freshness) {
    case 'fresh':
      return {
        label: 'FRESH ✓',
        badgeClass: 'bg-emerald-950/80 text-emerald-300 border border-emerald-700/60',
        dotColor: 'bg-emerald-400',
        icon: 'fas fa-check-circle',
      };
    case 'stale':
      return {
        label: 'STALE ◷',
        badgeClass: 'bg-amber-950/80 text-amber-300 border border-amber-700/60',
        dotColor: 'bg-amber-400',
        icon: 'fas fa-history',
      };
    case 'offline':
      return {
        label: 'OFFLINE ⊘',
        badgeClass: 'bg-rose-950/80 text-rose-300 border border-rose-700/60',
        dotColor: 'bg-rose-500',
        icon: 'fas fa-power-off',
      };
    case 'waiting':
      return {
        label: 'WAITING ⏳',
        badgeClass: 'bg-slate-800 text-slate-300 border border-slate-700',
        dotColor: 'bg-slate-400',
        icon: 'fas fa-hourglass-start',
      };
    case 'error':
      return {
        label: 'ERROR !',
        badgeClass: 'bg-rose-950 text-rose-200 border border-rose-600',
        dotColor: 'bg-rose-500',
        icon: 'fas fa-exclamation-triangle',
      };
    default:
      return {
        label: 'UNKNOWN',
        badgeClass: 'bg-slate-800 text-slate-400 border border-slate-700',
        dotColor: 'bg-slate-500',
        icon: 'fas fa-question-circle',
      };
  }
}

export function getDecisionBadge(
  bias: string | null | undefined,
  signalState: string | undefined | null
): StatusBadge {
  const isConfirmed = signalState === 'confirmed';

  if (!isConfirmed || !bias || bias === 'no_trade' || bias === 'neutral') {
    return {
      label: 'NO TRADE',
      badgeClass: 'bg-slate-800/90 text-slate-300 border border-slate-700 font-mono font-bold',
      dotColor: 'bg-slate-400',
      icon: 'fas fa-ban',
    };
  }

  if (bias === 'long') {
    return {
      label: 'LONG / BUY',
      badgeClass: 'bg-emerald-900/80 text-emerald-300 border border-emerald-600 font-mono font-bold',
      dotColor: 'bg-emerald-400',
      icon: 'fas fa-arrow-trend-up',
    };
  }

  if (bias === 'short') {
    return {
      label: 'SHORT / SELL',
      badgeClass: 'bg-rose-900/80 text-rose-300 border border-rose-600 font-mono font-bold',
      dotColor: 'bg-rose-400',
      icon: 'fas fa-arrow-trend-down',
    };
  }

  return {
    label: 'NO TRADE',
    badgeClass: 'bg-slate-800 text-slate-400 border border-slate-700 font-mono',
    dotColor: 'bg-slate-500',
    icon: 'fas fa-ban',
  };
}

export function getDeploymentModeBadge(mode: string | undefined | null): StatusBadge {
  switch (mode) {
    case 'live_systematic':
      return {
        label: 'LIVE REAL MONEY ⚠️',
        badgeClass: 'bg-emerald-950 text-emerald-300 border border-emerald-500 font-bold',
        dotColor: 'bg-emerald-400',
        icon: 'fas fa-bolt',
      };
    case 'demo_systematic':
      return {
        label: 'DEMO SYSTEMATIC',
        badgeClass: 'bg-indigo-950 text-indigo-300 border border-indigo-600',
        dotColor: 'bg-indigo-400',
        icon: 'fas fa-flask',
      };
    case 'paper':
    case 'paper_frozen':
      return {
        label: 'PAPER ACCUMULATION',
        badgeClass: 'bg-amber-950 text-amber-300 border border-amber-600',
        dotColor: 'bg-amber-400',
        icon: 'fas fa-file-invoice',
      };
    case 'research':
      return {
        label: 'RESEARCH ONLY (FROZEN)',
        badgeClass: 'bg-slate-800 text-slate-300 border border-slate-600',
        dotColor: 'bg-slate-400',
        icon: 'fas fa-microscope',
      };
    default:
      return {
        label: (mode || 'UNKNOWN').toUpperCase(),
        badgeClass: 'bg-slate-800 text-slate-400 border border-slate-700',
        dotColor: 'bg-slate-500',
        icon: 'fas fa-info-circle',
      };
  }
}

export function formatAge(ms: number | null | undefined): string {
  if (ms == null) return '—';
  const ageSec = Math.floor(Math.max(0, Date.now() - ms) / 1000);
  if (ageSec < 60) return `${ageSec}s ago`;
  const min = Math.floor(ageSec / 60);
  const sec = ageSec % 60;
  if (min < 60) return `${min}m ${sec.toString().padStart(2, '0')}s ago`;
  const hr = Math.floor(min / 60);
  return `${hr}h ${min % 60}m ago`;
}

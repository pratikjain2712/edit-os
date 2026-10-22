import React, { createContext, useCallback, useContext, useEffect, useRef, useState } from "react";
import { AnimatePresence, motion, useReducedMotion } from "framer-motion";
import { AlertCircle, CheckCircle2, Moon, Sun, MonitorSmartphone } from "lucide-react";

// --- theme -----------------------------------------------------------------------------------

const THEME_KEY = "editos.theme";

function readTheme() {
  try { return localStorage.getItem(THEME_KEY) || "system"; } catch { return "system"; }
}

export function applyTheme(t) {
  const root = document.documentElement;
  if (t === "light" || t === "dark") root.setAttribute("data-theme", t);
  else root.removeAttribute("data-theme");
}

export function useTheme() {
  const [theme, setTheme] = useState(readTheme);
  useEffect(() => {
    applyTheme(theme);
    try { localStorage.setItem(THEME_KEY, theme); } catch { /* private mode */ }
  }, [theme]);
  const next = useCallback(() => setTheme((t) => (t === "system" ? "light" : t === "light" ? "dark" : "system")), []);
  return [theme, next, setTheme];
}

export function ThemeButton({ theme, onClick }) {
  const Icon = theme === "dark" ? Moon : theme === "light" ? Sun : MonitorSmartphone;
  return (
    <button className="btn ghost icon" onClick={onClick} title={`Theme: ${theme} (click to change)`} aria-label="Change theme">
      <Icon size={17} />
    </button>
  );
}

// --- toasts ----------------------------------------------------------------------------------

const ToastCtx = createContext(() => {});
export const useToast = () => useContext(ToastCtx);

export function ToastProvider({ children }) {
  const [items, setItems] = useState([]);
  const n = useRef(0);
  // `action`: optional {label, onClick} button on the toast (e.g. Undo); the toast closes when it is used.
  const push = useCallback((text, kind = "ok", ms = 2600, action = null) => {
    const id = ++n.current;
    setItems((xs) => [...xs.slice(-2), { id, text, kind, action }]);
    setTimeout(() => setItems((xs) => xs.filter((x) => x.id !== id)), kind === "error" ? Math.max(ms, 5200) : ms);
  }, []);
  return (
    <ToastCtx.Provider value={push}>
      {children}
      <div className="toasts" role="status" aria-live="polite">
        <AnimatePresence>
          {items.map((t) => (
            <motion.div key={t.id} className={`toast ${t.kind}`} layout
              initial={{ opacity: 0, y: 16, scale: 0.96 }} animate={{ opacity: 1, y: 0, scale: 1 }}
              exit={{ opacity: 0, y: 8, scale: 0.98 }} transition={{ type: "spring", stiffness: 420, damping: 32 }}>
              {t.kind === "error" ? <AlertCircle size={16} className="ico" /> : <CheckCircle2 size={16} className="ico" />}
              <span>{t.text}</span>
              {t.action && (
                <button className="toast-act" onClick={() => { t.action.onClick(); setItems((xs) => xs.filter((x) => x.id !== t.id)); }}>
                  {t.action.label}
                </button>
              )}
            </motion.div>
          ))}
        </AnimatePresence>
      </div>
    </ToastCtx.Provider>
  );
}

// --- bits ------------------------------------------------------------------------------------

export function ProgressRing({ done, total, size = 30 }) {
  const r = (size - 5) / 2;
  const c = 2 * Math.PI * r;
  const frac = total ? done / total : 0;
  return (
    <span className="ring" title={`${done} of ${total} decided`}>
      <svg width={size} height={size} aria-hidden="true">
        <defs>
          <linearGradient id="ringgrad" x1="0" y1="0" x2="1" y2="1">
            <stop offset="0%" stopColor="var(--accent)" />
            <stop offset="100%" stopColor="var(--ok)" />
          </linearGradient>
        </defs>
        <circle className="track" cx={size / 2} cy={size / 2} r={r} fill="none" strokeWidth="4" />
        <circle className="bar" cx={size / 2} cy={size / 2} r={r} fill="none" strokeWidth="4" strokeLinecap="round"
          strokeDasharray={c} strokeDashoffset={c * (1 - frac)} />
      </svg>
      <span><b>{Math.round(frac * 100)}%</b> <span className="label">decided · {done}/{total}</span></span>
    </span>
  );
}

/** A number that ticks up to its value. */
export function Ticker({ value, duration = 900 }) {
  const reduce = useReducedMotion();
  const [shown, setShown] = useState(value);
  const from = useRef(value);
  useEffect(() => {
    if (reduce) { setShown(value); return; }
    const start = performance.now();
    const a = from.current;
    let raf;
    const step = (t) => {
      const k = Math.min(1, (t - start) / duration);
      const eased = 1 - Math.pow(1 - k, 3);
      setShown(Math.round(a + (value - a) * eased));
      if (k < 1) raf = requestAnimationFrame(step);
      else from.current = value;
    };
    raf = requestAnimationFrame(step);
    return () => { cancelAnimationFrame(raf); from.current = value; };
  }, [value, duration, reduce]);
  return <>{shown}</>;
}

export function Sparkle({ size = 10 }) {
  return (
    <svg width={size} height={size} viewBox="0 0 10 10" aria-hidden="true">
      <path d="M5 0 C5.4 3.2 6.8 4.6 10 5 C6.8 5.4 5.4 6.8 5 10 C4.6 6.8 3.2 5.4 0 5 C3.2 4.6 4.6 3.2 5 0Z" fill="currentColor" />
    </svg>
  );
}

export function useMedia(query) {
  const get = () => typeof window !== "undefined" && window.matchMedia(query).matches;
  const [m, setM] = useState(get);
  useEffect(() => {
    const mq = window.matchMedia(query);
    const on = () => setM(mq.matches);
    mq.addEventListener("change", on);
    return () => mq.removeEventListener("change", on);
  }, [query]);
  return m;
}

export function useOutside(ref, onOut, active = true) {
  useEffect(() => {
    if (!active) return undefined;
    const h = (e) => { if (ref.current && !ref.current.contains(e.target)) onOut(e); };
    document.addEventListener("mousedown", h);
    return () => document.removeEventListener("mousedown", h);
  }, [ref, onOut, active]);
}

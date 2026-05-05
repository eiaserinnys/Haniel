import type { CSSProperties } from 'react';

interface IconProps {
  name: string;
  size?: number;
  style?: CSSProperties;
  className?: string;
}

/**
 * SVG icon component — stroke-based, monochromatic, currentColor.
 * Ported from prototype utils.jsx.
 */
export function Icon({ name, size = 14, style, className }: IconProps) {
  const common = {
    width: size,
    height: size,
    viewBox: '0 0 16 16',
    fill: 'none',
    stroke: 'currentColor',
    strokeWidth: 1.4,
    strokeLinecap: 'round' as const,
    strokeLinejoin: 'round' as const,
    style: { display: 'inline-block', flexShrink: 0, ...style },
    className,
  };

  switch (name) {
    case 'inbox':
      return <svg {...common}><path d="M2.5 9.5v3a1 1 0 0 0 1 1h9a1 1 0 0 0 1-1v-3" /><path d="M2.5 9.5 4 3.5h8l1.5 6" /><path d="M2.5 9.5h2.8L6 11h4l.7-1.5h2.8" /></svg>;
    case 'server':
      return <svg {...common}><rect x="2.5" y="2.5" width="11" height="4.5" rx="1" /><rect x="2.5" y="9" width="11" height="4.5" rx="1" /><circle cx="5" cy="4.7" r=".4" fill="currentColor" stroke="none" /><circle cx="5" cy="11.2" r=".4" fill="currentColor" stroke="none" /></svg>;
    case 'history':
      return <svg {...common}><path d="M3 8a5 5 0 1 0 1.5-3.5" /><path d="M3 3v2.5h2.5" /><path d="M8 5.5V8l1.8 1.2" /></svg>;
    case 'check':
      return <svg {...common}><path d="m3.5 8.2 3 3 6-6.4" /></svg>;
    case 'x':
      return <svg {...common}><path d="m4 4 8 8M12 4l-8 8" /></svg>;
    case 'chevron':
      return <svg {...common}><path d="m5 6 3 3 3-3" /></svg>;
    case 'chevron-r':
      return <svg {...common}><path d="m6 4 4 4-4 4" /></svg>;
    case 'commit':
      return <svg {...common}><circle cx="8" cy="8" r="2.5" /><path d="M2 8h3.5M10.5 8H14" /></svg>;
    case 'branch':
      return <svg {...common}><circle cx="4" cy="3.5" r="1.3" /><circle cx="4" cy="12.5" r="1.3" /><circle cx="12" cy="6" r="1.3" /><path d="M4 4.8v6.4M4 8.5c0-1.5 1-2.5 2.5-2.5h4" /></svg>;
    case 'dot':
      return <svg {...common}><circle cx="8" cy="8" r="3" fill="currentColor" stroke="none" /></svg>;
    case 'bell':
      return <svg {...common}><path d="M4 11.5h8M5.5 11.5V7a2.5 2.5 0 0 1 5 0v4.5M7 13.5h2" /></svg>;
    case 'search':
      return <svg {...common}><circle cx="7" cy="7" r="3.5" /><path d="m9.7 9.7 3 3" /></svg>;
    case 'play':
      return <svg {...common}><path d="M5 3.5v9l7-4.5z" /></svg>;
    case 'loader':
      return <svg {...common}><circle cx="8" cy="8" r="5.5" strokeDasharray="6 18" /></svg>;
    case 'wifi-off':
      return <svg {...common}><path d="m3 3 10 10M2 6.5a8 8 0 0 1 3-2M11 4.5a8 8 0 0 1 3 2M5 9a4 4 0 0 1 1.5-1M9.5 8a4 4 0 0 1 1.5 1" /><circle cx="8" cy="11.5" r=".7" fill="currentColor" stroke="none" /></svg>;
    case 'cpu':
      return <svg {...common}><rect x="4" y="4" width="8" height="8" rx="1" /><rect x="6.5" y="6.5" width="3" height="3" rx=".4" /><path d="M4 6.5H2.5M4 9.5H2.5M12 6.5h1.5M12 9.5h1.5M6.5 4V2.5M9.5 4V2.5M6.5 12v1.5M9.5 12v1.5" /></svg>;
    case 'linux':
      return <svg {...common}><path d="M5.5 3.5v3M10.5 3.5v3" /><path d="M5 6.5c-.5 1.5-2 2.5-2 4.5 0 1.5 2 2.5 5 2.5s5-1 5-2.5c0-2-1.5-3-2-4.5" /><circle cx="6.5" cy="5.5" r=".5" fill="currentColor" stroke="none" /><circle cx="9.5" cy="5.5" r=".5" fill="currentColor" stroke="none" /></svg>;
    case 'windows':
      return <svg {...common}><path d="M2.5 4.2 7 3.5v4H2.5zM7 3.5l6.5-1v5H7zM2.5 8.5H7v4l-4.5-.7zM7 8.5h6.5v5L7 12.5z" /></svg>;
    case 'plus':
      return <svg {...common}><path d="M8 3.5v9M3.5 8h9" /></svg>;
    case 'menu':
      return <svg {...common}><path d="M3 5h10M3 8h10M3 11h10" /></svg>;
    case 'sun':
      return <svg {...common}><circle cx="8" cy="8" r="2.6" /><path d="M8 2v1.4M8 12.6V14M2 8h1.4M12.6 8H14M3.76 3.76l1 1M11.24 11.24l1 1M12.24 3.76l-1 1M4.76 11.24l-1 1" /></svg>;
    case 'moon':
      return <svg {...common}><path d="M12.5 9.5A5 5 0 1 1 6.5 3.5a4 4 0 0 0 6 6z" /></svg>;
    case 'filter':
      return <svg {...common}><path d="M3 4h10l-3.5 5v3.5L6.5 11.5V9z" /></svg>;
    case 'arrow-r':
      return <svg {...common}><path d="M3 8h10M9.5 4.5 13 8l-3.5 3.5" /></svg>;
    case 'refresh':
      return <svg {...common}><path d="M3 8a5 5 0 0 1 9-3M13 8a5 5 0 0 1-9 3" /><path d="M12 2v3h-3M4 14v-3h3" /></svg>;
    case 'stop':
      return <svg {...common}><rect x="4" y="4" width="8" height="8" rx="1" /></svg>;
    default:
      return null;
  }
}

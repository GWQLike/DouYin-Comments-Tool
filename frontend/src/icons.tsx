import type { CSSProperties } from 'react';

type IconName = 'chat' | 'search' | 'link' | 'arrow' | 'download' | 'play' | 'stop' | 'refresh' | 'close' | 'check' | 'clock' | 'chevron' | 'trash' | 'external' | 'shield' | 'login' | 'info' | 'plus' | 'heart' | 'user' | 'recommend';

const paths: Record<IconName, React.ReactNode> = {
  chat: <><path d="M20 11.5a7.5 7.5 0 0 1-7.5 7.5H5l-3 3v-9.5A7.5 7.5 0 0 1 9.5 5h3A7.5 7.5 0 0 1 20 12"/><path d="M8 10h6M8 14h9M15 2h7v7"/></>,
  search: <><circle cx="10.5" cy="10.5" r="6.5"/><path d="m16 16 5 5"/></>,
  link: <><path d="m10 13 4-4M8 16l-1 1a4.2 4.2 0 0 1-6-6l5-5a4.2 4.2 0 0 1 6 0M16 8l1-1a4.2 4.2 0 0 1 6 6l-5 5a4.2 4.2 0 0 1-6 0" transform="translate(0 -1) scale(.94)"/></>,
  arrow: <><path d="M4 12h15m-6-6 6 6-6 6"/></>,
  download: <><path d="M12 3v12m-5-5 5 5 5-5M4 16v4h16v-4"/></>,
  play: <path d="m8 5 11 7-11 7Z"/>,
  stop: <rect x="6" y="6" width="12" height="12" rx="2"/>,
  refresh: <><path d="M20 7v5h-5M4 17v-5h5M5.5 7a7.5 7.5 0 0 1 13-2l1.5 2M4 17l1.5 2a7.5 7.5 0 0 0 13-2"/></>,
  close: <path d="m6 6 12 12M6 18 18 6"/>,
  check: <path d="m5 12 4 4L19 6"/>,
  clock: <><circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/></>,
  chevron: <path d="m9 5 7 7-7 7"/>,
  trash: <><path d="M4 6h16M9 6V3h6v3M6 6l1 15h10l1-15M10 10v7M14 10v7"/></>,
  external: <><path d="M14 3h7v7m0-7L10 14M10 3H3v18h18v-7"/></>,
  shield: <><path d="m12 3 8 3v6c0 5-8 9-8 9s-8-4-8-9V6Z"/><path d="m8 12 3 3 5-6"/></>,
  login: <><path d="M9 3H3v6m12-6h6v6M3 15v6h6m12-6v6h-6"/><rect x="8" y="8" width="3" height="3"/><path d="M15 8v4h-3v4H8m7 0h2v2"/></>,
  info: <><circle cx="12" cy="12" r="9"/><path d="M12 11v6m0-10v.1"/></>,
  plus: <path d="M12 5v14M5 12h14"/>,
  heart: <path d="M20.5 4.8a5 5 0 0 0-7.1 0L12 6.2l-1.4-1.4a5 5 0 0 0-7.1 7.1L12 20l8.5-8.1a5 5 0 0 0 0-7.1Z"/>,
  user: <><circle cx="12" cy="8" r="4"/><path d="M4 21v-2a8 8 0 0 1 16 0v2"/></>,
  recommend: <><path d="M8 10l4-7a2 2 0 0 1 2 2v4h5a2 2 0 0 1 2 2l-2 8a2 2 0 0 1-2 2H8Z"/><path d="M3 10h5v11H3Z"/></>,
};

export function Icon({ name, size = 18, className = '', style }: { name: IconName; size?: number; className?: string; style?: CSSProperties }) {
  return <svg width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.65" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true" className={className} style={style}>{paths[name]}</svg>;
}

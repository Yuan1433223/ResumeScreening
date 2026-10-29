const paths = {
  home: 'M3 10 12 3l9 7v10a1 1 0 0 1-1 1h-5v-7H9v7H4a1 1 0 0 1-1-1Z',
  files: 'M8 3h10l3 3v14H8ZM3 7v14h13M16 3v5h5M11 12h7M11 16h5',
  calendar: 'M4 5h16v16H4ZM7 2v6M17 2v6M4 10h16M8 14h2M14 14h2M8 18h2',
  activity: 'M3 12h4l3-8 4 16 3-8h4',
  chart: 'M4 3v18h17M8 16v-5M13 16V7M18 16v-9',
  arrow: 'M4 12h16M14 6l6 6-6 6',
  check: 'M5 12l4 4L19 6',
  shield: 'M12 3l8 3v6c0 5-8 9-8 9s-8-4-8-9V6ZM8 12l3 3 5-6',
  retry: 'M20 7a8 8 0 1 0 1 8M20 3v5h-5',
  search: 'M11 18a7 7 0 1 0 0-14 7 7 0 0 0 0 14ZM16 16l5 5',
} as const
export default function Icon({ name, size = 18 }: { name: keyof typeof paths; size?: number }) {
  return <svg width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true"><path d={paths[name]} /></svg>
}

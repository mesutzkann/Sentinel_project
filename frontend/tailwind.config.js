/** @type {import('tailwindcss').Config} */
export default {
  content: ['./index.html', './src/**/*.{ts,tsx}'],
  theme: {
    extend: {
      colors: {
        // Deep slate ground with a cool cast. This is a console people stare at during an
        // incident, so the palette stays quiet except where severity has to shout.
        ink: {
          950: '#0a0e17',
          900: '#0f1420',
          850: '#141a28',
          800: '#1a2130',
          700: '#252d3d',
          600: '#38414f',
        },
        accent: {
          DEFAULT: '#4f9cf9',
          muted: '#2a4a73',
        },
        // Ordered by heat, so severity reads as a gradient rather than four unrelated colours.
        severity: {
          low: '#5aa9e6',
          medium: '#e8b04b',
          high: '#e8794b',
          critical: '#e8556d',
        },
        state: {
          ok: '#4ec9a5',
          warn: '#e8b04b',
          bad: '#e8556d',
        },
      },
      fontFamily: {
        sans: ['Inter', 'ui-sans-serif', 'system-ui', 'sans-serif'],
        mono: ['JetBrains Mono', 'ui-monospace', 'SFMono-Regular', 'monospace'],
      },
    },
  },
  plugins: [],
};

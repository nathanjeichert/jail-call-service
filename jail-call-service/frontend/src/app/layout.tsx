import type { Metadata } from 'next';
import './globals.css';

export const metadata: Metadata = {
  title: 'Jail Call Service',
  description: 'Jail call transcription and packaging service',
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en">
      <body className="bg-slate-100 text-slate-900 min-h-screen">{children}</body>
    </html>
  );
}

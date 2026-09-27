import type { Metadata } from "next";

import "./globals.css";

export const metadata: Metadata = {
  title: "DarwinUX",
  description: "Software that learns how to redesign itself.",
};

export default function RootLayout({ children }: LayoutProps<"/">) {
  return (
    <html lang="en">
      <body>{children}</body>
    </html>
  );
}

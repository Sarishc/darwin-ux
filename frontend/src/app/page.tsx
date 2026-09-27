import Link from "next/link";

export default function Home() {
  return (
    <main className="spec-page">
      <section className="section space-lg">
        <h1>DarwinUX</h1>
        <p className="text emphasis-normal">
          Software that learns how to redesign itself. This app hosts the product surfaces
          DarwinUX observes and, later, evolves.
        </p>
        <p className="text emphasis-normal">
          <Link href="/demo">Open the Generation 0 demo →</Link>
        </p>
      </section>
    </main>
  );
}

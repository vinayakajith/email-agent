/**
 * Turns bursty token delivery into an even reveal.
 *
 * Text is revealed at a rate that grows with the backlog (about 45 chars/s when keeping up,
 * faster when behind), aligned to word boundaries so words never appear half-typed. Once the
 * stream is finished, whatever remains is revealed within ~150 ms.
 */
export class Smoother {
  private target = "";
  private shown = 0;
  private done = false;
  private finishRate = 0;

  static readonly BASE_RATE = 45; // chars per second when caught up
  static readonly BACKLOG_GAIN = 2.4; // extra chars/s per char of backlog
  static readonly MAX_RATE = 420;
  static readonly FINISH_MS = 150;

  push(text: string): void {
    this.target += text;
  }

  /** Mark the stream complete; optionally replace the text with the authoritative final version. */
  finish(finalText?: string): void {
    if (finalText !== undefined && finalText.startsWith(this.target.slice(0, this.shown))) this.target = finalText;
    else if (finalText !== undefined) {
      this.target = finalText;
      this.shown = Math.min(this.shown, finalText.length);
    }
    this.done = true;
    // A constant catch-up speed (not one recomputed from a shrinking backlog) lands on time.
    this.finishRate = (this.target.length - this.shown) / (Smoother.FINISH_MS / 1000);
  }

  reset(): void {
    this.target = "";
    this.shown = 0;
    this.done = false;
    this.finishRate = 0;
  }

  get settled(): boolean {
    return this.done && this.shown >= this.target.length;
  }

  get fullText(): string {
    return this.target;
  }

  /** Advance by `dtMs` and return the text to display. */
  step(dtMs: number): string {
    const backlog = this.target.length - this.shown;
    if (backlog > 0) {
      let rate = Math.min(Smoother.MAX_RATE, Smoother.BASE_RATE + backlog * Smoother.BACKLOG_GAIN);
      if (this.done) rate = Math.max(rate, this.finishRate);
      this.shown = Math.min(this.target.length, this.shown + (rate * dtMs) / 1000);
    }
    return this.visible();
  }

  visible(): string {
    const end = Math.floor(this.shown);
    if (this.done && end >= this.target.length) return this.target;
    // Reveal whole words only: cut back to the last whitespace (or show nothing of a first word yet).
    const slice = this.target.slice(0, end);
    const cut = slice.search(/\s\S*$/);
    return cut === -1 ? "" : slice.slice(0, cut + 1);
  }
}

/** Hide syntax that would flash while incomplete: a half-written citation, or an unclosed bold/code span. */
export function renderable(text: string): string {
  let out = text.replace(/\[#?\d*$/, "");
  if ((out.match(/\*\*/g) ?? []).length % 2) out += "**";
  if ((out.match(/`/g) ?? []).length % 2) out += "`";
  return out;
}

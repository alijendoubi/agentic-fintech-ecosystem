/** Server wall clock in epoch milliseconds. Kept out of components so render code stays free of impure calls. */
export function serverNowMs(): number {
  return Date.now();
}

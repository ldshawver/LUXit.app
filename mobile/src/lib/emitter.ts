export type Listener<T> = (value: T) => void;

/** Minimal synchronous observable used by the engine and stores. */
export class Observable<T> {
  private listeners = new Set<Listener<T>>();

  subscribe(listener: Listener<T>): () => void {
    this.listeners.add(listener);
    return () => {
      this.listeners.delete(listener);
    };
  }

  get size(): number {
    return this.listeners.size;
  }

  protected emit(value: T): void {
    for (const l of Array.from(this.listeners)) {
      try {
        l(value);
      } catch (e) {
        // A broken UI subscriber must never break call handling.
        console.warn('listener failed', e);
      }
    }
  }
}

//! Bounded drop-oldest queue that decouples the WebSocket read loop from slow
//! sinks (QuestDB, Redis). `push` never blocks and never awaits: when a sink is
//! down the queue keeps the *newest* items and counts what it dropped, so an
//! outage can never stall market-data ingestion.

use std::collections::VecDeque;
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::Mutex;
use std::time::Duration;

use tokio::sync::Notify;

pub struct BoundedQueue<T> {
    inner: Mutex<VecDeque<T>>,
    notify: Notify,
    capacity: usize,
    dropped: AtomicU64,
}

impl<T> BoundedQueue<T> {
    pub fn new(capacity: usize) -> Self {
        let capacity = capacity.max(1);
        Self {
            inner: Mutex::new(VecDeque::with_capacity(capacity.min(4_096))),
            notify: Notify::new(),
            capacity,
            dropped: AtomicU64::new(0),
        }
    }

    fn lock(&self) -> std::sync::MutexGuard<'_, VecDeque<T>> {
        // A poisoned lock only means another thread panicked mid-push; the
        // deque itself is still structurally valid.
        self.inner.lock().unwrap_or_else(|p| p.into_inner())
    }

    /// Enqueue; if full, the oldest item is dropped. Returns `true` when an
    /// item was dropped to make room.
    pub fn push(&self, item: T) -> bool {
        let dropped = {
            let mut q = self.lock();
            let full = q.len() >= self.capacity;
            if full {
                q.pop_front();
            }
            q.push_back(item);
            full
        };
        if dropped {
            self.dropped.fetch_add(1, Ordering::Relaxed);
        }
        self.notify.notify_one();
        dropped
    }

    /// Put previously popped (older) items back at the front, keeping order.
    /// If that overflows the bound, the oldest items are dropped first.
    pub fn requeue_front(&self, items: Vec<T>) {
        if items.is_empty() {
            return;
        }
        let mut lost = 0_u64;
        {
            let mut q = self.lock();
            for item in items.into_iter().rev() {
                q.push_front(item);
            }
            while q.len() > self.capacity {
                q.pop_front();
                lost += 1;
            }
        }
        if lost > 0 {
            self.dropped.fetch_add(lost, Ordering::Relaxed);
        }
        self.notify.notify_one();
    }

    /// Move up to `max` items into `out` without waiting.
    pub fn drain_into(&self, max: usize, out: &mut Vec<T>) {
        let mut q = self.lock();
        let n = max.min(q.len());
        out.extend(q.drain(..n));
    }

    /// Wait until at least one item is available, then take up to `max`.
    pub async fn pop_batch(&self, max: usize, out: &mut Vec<T>) {
        loop {
            self.drain_into(max, out);
            if !out.is_empty() {
                return;
            }
            self.notify.notified().await;
        }
    }

    /// Like `pop_batch`, but after the first item lingers up to `linger` for
    /// more (stopping early once `max` items are collected).
    pub async fn pop_batch_lingering(&self, max: usize, linger: Duration, out: &mut Vec<T>) {
        self.pop_batch(max, out).await;
        if out.len() >= max {
            return;
        }
        tokio::time::sleep(linger).await;
        self.drain_into(max - out.len(), out);
    }

    pub fn len(&self) -> usize {
        self.lock().len()
    }

    pub fn is_empty(&self) -> bool {
        self.lock().is_empty()
    }

    pub fn dropped(&self) -> u64 {
        self.dropped.load(Ordering::Relaxed)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::Arc;

    #[test]
    fn drops_oldest_when_full_and_counts() {
        let q = BoundedQueue::new(3);
        assert!(!q.push(1));
        assert!(!q.push(2));
        assert!(!q.push(3));
        assert!(q.push(4));
        assert!(q.push(5));
        assert_eq!(q.dropped(), 2);
        let mut out = Vec::new();
        q.drain_into(10, &mut out);
        assert_eq!(out, vec![3, 4, 5]);
        assert!(q.is_empty());
    }

    #[test]
    fn requeue_front_keeps_order_and_bounds() {
        let q = BoundedQueue::new(4);
        q.push(5);
        q.push(6);
        q.requeue_front(vec![1, 2, 3]);
        // 5 items into capacity 4: the oldest (1) is dropped.
        let mut out = Vec::new();
        q.drain_into(10, &mut out);
        assert_eq!(out, vec![2, 3, 5, 6]);
        assert_eq!(q.dropped(), 1);
    }

    #[test]
    fn push_never_blocks_with_no_consumer() {
        let q = BoundedQueue::new(8);
        for i in 0..100_000 {
            q.push(i);
        }
        assert_eq!(q.len(), 8);
        assert_eq!(q.dropped(), 100_000 - 8);
    }

    #[tokio::test]
    async fn pop_batch_waits_for_data() {
        let q = Arc::new(BoundedQueue::new(8));
        let q2 = Arc::clone(&q);
        let waiter = tokio::spawn(async move {
            let mut out = Vec::new();
            q2.pop_batch(4, &mut out).await;
            out
        });
        tokio::time::sleep(Duration::from_millis(20)).await;
        q.push(7);
        let out = tokio::time::timeout(Duration::from_secs(2), waiter)
            .await
            .expect("no timeout")
            .expect("join");
        assert_eq!(out, vec![7]);
    }

    #[tokio::test]
    async fn lingering_batch_collects_followers() {
        let q = Arc::new(BoundedQueue::new(16));
        q.push(1);
        let q2 = Arc::clone(&q);
        tokio::spawn(async move {
            tokio::time::sleep(Duration::from_millis(5)).await;
            q2.push(2);
            q2.push(3);
        });
        let mut out = Vec::new();
        q.pop_batch_lingering(10, Duration::from_millis(50), &mut out)
            .await;
        assert_eq!(out, vec![1, 2, 3]);
    }
}

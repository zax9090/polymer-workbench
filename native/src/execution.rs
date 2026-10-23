//! FIFO admission for all heavyweight work in one application instance.
use std::collections::{HashSet, VecDeque};

pub struct ExecutionQueue {
    waiting: VecDeque<String>,
    active: HashSet<String>,
}

impl ExecutionQueue {
    pub fn new() -> Self { Self { waiting: VecDeque::new(), active: HashSet::new() } }
    pub fn enqueue(&mut self, id: String) -> Result<(), String> {
        if self.waiting.len() >= 256 { return Err("Execution queue is full".into()); }
        if self.active.contains(&id) || self.waiting.contains(&id) { return Err("Duplicate execution ticket".into()); }
        self.waiting.push_back(id);
        Ok(())
    }
    pub fn acquire(&mut self, id: &str) -> bool {
        if self.active.is_empty() && self.waiting.front().is_some_and(|next| next == id) {
            self.waiting.pop_front();
            self.active.insert(id.to_owned());
            true
        } else { false }
    }
    pub fn release(&mut self, id: &str) { self.active.remove(id); self.waiting.retain(|x| x != id); }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn fifo_and_exclusion_survive_cancellation() {
        let mut q = ExecutionQueue::new();
        for id in ["md", "doe", "volume"] { q.enqueue(id.into()).unwrap(); }
        assert!(!q.acquire("doe"));
        assert!(q.acquire("md"));
        assert!(!q.acquire("doe"));
        q.release("doe"); // cancel a waiter without releasing the active lease
        assert!(!q.acquire("volume"));
        q.release("md");
        assert!(q.acquire("volume"));
        assert!(q.enqueue("volume".into()).is_err());
        q.release("volume");
        assert!(!q.acquire("absent"));
    }
}

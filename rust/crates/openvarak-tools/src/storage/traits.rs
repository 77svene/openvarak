//! MemoryBackend trait for all storage backends.

use openvarak_core::{OpenVarakError, RetrievalResult};
use serde_json::Value;

pub trait MemoryBackend: Send + Sync {
    fn backend_id(&self) -> &str;
    fn store(
        &self,
        content: &str,
        source: &str,
        metadata: Option<&Value>,
    ) -> Result<String, OpenVarakError>;
    fn retrieve(
        &self,
        query: &str,
        top_k: usize,
    ) -> Result<Vec<RetrievalResult>, OpenVarakError>;
    fn delete(&self, doc_id: &str) -> Result<bool, OpenVarakError>;
    fn clear(&self) -> Result<(), OpenVarakError>;
    fn count(&self) -> Result<usize, OpenVarakError>;
}

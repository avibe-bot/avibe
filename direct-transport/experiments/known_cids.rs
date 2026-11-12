// CI-only source, copied into disposable vendored iroh by the advisory script.
// Predictable CIDs are deliberately unsuitable for production. Only the probe
// client opts in; separately spawned sidecars retain their default generator.
use std::sync::{Arc, Mutex, OnceLock};

const CID_PREFIX: &[u8; 16] = b"AVIBE-PROBE-CID-";

pub type Issued = Arc<Mutex<Vec<Vec<u8>>>>;
pub(crate) static ISSUED: OnceLock<Issued> = OnceLock::new();

pub fn enable() -> Issued {
    assert_eq!(
        std::env::var("AVDT_ISOLATED_EXPERIMENT").as_deref(),
        Ok("1")
    );
    let issued = Issued::default();
    ISSUED.set(issued.clone()).expect("one probe endpoint only");
    issued
}

pub(crate) struct KnownCidGenerator(pub Issued);

impl noq::ConnectionIdGenerator for KnownCidGenerator {
    fn generate_cid(&mut self) -> noq::ConnectionId {
        let mut issued = self.0.lock().unwrap();
        let sequence = u32::try_from(issued.len()).unwrap();
        let mut bytes = [0u8; 20];
        bytes[..16].copy_from_slice(CID_PREFIX);
        bytes[16..].copy_from_slice(&sequence.to_be_bytes());
        issued.push(bytes.to_vec());
        noq::ConnectionId::new(&bytes)
    }

    fn cid_len(&self) -> usize {
        20
    }

    fn cid_lifetime(&self) -> Option<std::time::Duration> {
        None
    }
}

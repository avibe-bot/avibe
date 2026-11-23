// Compiled only in the unit-test executable. Neither the test CA nor the
// loopback relay selector is a feature or an environment input to the binary.
use super::*;
use tokio::io::AsyncBufReadExt;

fn url() -> RelayUrl {
    "https://127.0.0.1:33443/".parse().unwrap()
}
pub(super) fn trust() -> iroh_relay::tls::CaTlsConfig {
    iroh_relay::tls::CaTlsConfig::custom_roots([std::fs::read("ca.der").unwrap().into()])
}

#[tokio::test]
#[ignore = "child of isolated relay interop fixture"]
async fn sidecar() {
    let fixture: TestRelay = (RelayMap::from(url()), |builder| {
        builder.ca_tls_config(trust())
    });
    run(&Control::new(), Some(fixture)).await.unwrap();
    // stdin uses a blocking Tokio reader; same shutdown model as main.
    std::process::exit(0);
}

#[tokio::test]
#[ignore = "child of isolated relay interop fixture"]
async fn client() {
    let mut report = NetReportConfig::minimal();
    report.https_probes = true;
    let ep = Endpoint::builder(presets::Minimal)
        .relay_mode(RelayMode::Custom(RelayMap::from(url())))
        .portmapper_config(PortmapperConfig::Disabled)
        .net_report_config(report)
        .ca_tls_config(trust())
        .bind()
        .await
        .unwrap();
    assert!(ep.address_lookup().unwrap().is_empty());
    println!("CLIENT {}", ep.id());
    let mut input = BufReader::new(tokio::io::stdin()).lines();
    let id = input.next_line().await.unwrap().unwrap().parse().unwrap();
    let conn = ep
        .connect(EndpointAddr::new(id).with_relay_url(url()), ALPN.as_bytes())
        .await
        .unwrap();
    let selected = || {
        let paths = conn.paths();
        let chosen: Vec<_> = paths.iter().filter(|p| p.is_selected()).collect();
        assert_eq!(chosen.len(), 1);
        assert!(chosen[0].is_relay());
    };
    let bridge = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
    println!("BRIDGE {}", bridge.local_addr().unwrap().port());
    let (tcp, _) = bridge.accept().await.unwrap();
    let (mut upstream, mut downstream) = conn.open_bi().await.unwrap();
    let (mut read, mut write) = tcp.into_split();
    selected();
    // The driver holds its bridge open until it verifies the response.
    tokio::select! {
        res = tokio::io::copy(&mut read, &mut upstream) => { res.unwrap(); },
        res = tokio::io::copy(&mut downstream, &mut write) => { res.unwrap(); },
    }
    selected();
    println!("PATH local-relay");
    ep.close().await;
}

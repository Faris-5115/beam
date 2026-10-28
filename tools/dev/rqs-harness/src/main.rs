// Interop test peer for multidrop: drives rquickshare's core library.
//   rqs-harness recv <port> <download-dir> [reject]
//   rqs-harness send <ip:port> <file>...
// Runs with Visibility::Invisible, so nothing is announced over mDNS.
use rqs_lib::channel::{ChannelAction, ChannelDirection, ChannelMessage};
use rqs_lib::{OutboundPayload, SendInfo, State, Visibility, RQS};
use std::path::PathBuf;
use std::time::Duration;

#[tokio::main]
async fn main() -> anyhow::Result<()> {
    tracing_subscriber::fmt()
        .with_env_filter(std::env::var("RUST_LOG").unwrap_or_else(|_| "info,mdns_sd=error".into()))
        .with_writer(std::io::stderr)
        .init();
    let args: Vec<String> = std::env::args().collect();
    let mode = args.get(1).map(String::as_str).unwrap_or("");
    let mut rqs;
    let send_ch;
    match mode {
        "recv" => {
            let port: u32 = args[2].parse()?;
            rqs = RQS::new(Visibility::Invisible, Some(port), Some(PathBuf::from(&args[3])));
            send_ch = rqs.run().await?.0;
            println!("READY");
        }
        "send" => {
            rqs = RQS::new(Visibility::Invisible, None, None);
            send_ch = rqs.run().await?.0;
            send_ch
                .send(SendInfo {
                    id: "multidrop-test".into(),
                    name: "multidrop".into(),
                    addr: args[2].clone(),
                    ob: OutboundPayload::Files(args[3..].to_vec()),
                })
                .await?;
        }
        _ => anyhow::bail!("usage: rqs-harness recv <port> <dir> [reject] | send <ip:port> <file>..."),
    }
    let reject = args.get(4).map(String::as_str) == Some("reject");
    let mut rx = rqs.message_sender.subscribe();
    let result = tokio::time::timeout(Duration::from_secs(60), async {
        loop {
            let msg = rx.recv().await?;
            if msg.direction != ChannelDirection::LibToFront {
                continue;
            }
            println!("EVENT state={:?} meta={:?}", msg.state, msg.meta);
            match msg.state {
                Some(State::WaitingForUserConsent) if mode == "recv" => {
                    let action = if reject { ChannelAction::RejectTransfer } else { ChannelAction::AcceptTransfer };
                    rqs.message_sender.send(ChannelMessage {
                        id: msg.id.clone(),
                        direction: ChannelDirection::FrontToLib,
                        action: Some(action),
                        ..Default::default()
                    })?;
                }
                Some(State::Finished) => return Ok::<_, anyhow::Error>("FINISHED"),
                Some(State::Rejected) => {
                    // rquickshare reports Rejected before it sends the frame.
                    tokio::time::sleep(Duration::from_secs(1)).await;
                    return Ok("REJECTED");
                }
                Some(State::Cancelled) => return Ok("CANCELLED"),
                Some(State::Disconnected) => return Ok("DISCONNECTED"),
                _ => {}
            }
        }
    })
    .await;
    drop(send_ch);
    match result {
        Ok(Ok(r)) => println!("RESULT {r}"),
        Ok(Err(e)) => println!("RESULT ERROR {e}"),
        Err(_) => println!("RESULT TIMEOUT"),
    }
    rqs.stop().await;
    Ok(())
}

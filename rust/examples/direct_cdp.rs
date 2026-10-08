use mimic_sdk::{generated::GetVersionParams, Client, Transport};
use serde_json::json;

#[tokio::main]
async fn main() -> Result<(), Box<dyn std::error::Error>> {
    let endpoint = std::env::var("MIMIC_CDP_ENDPOINT")?;
    let client = Client::new(Transport::connect(&endpoint).await?);
    println!(
        "Mimic {}",
        client.get_version(GetVersionParams {}).await?.version
    );
    let targets = client.call("Target.getTargets", Some(json!({}))).await?;
    println!("{targets}");
    client.transport.close().await;
    Ok(())
}

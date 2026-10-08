use mimic_browser::{RuntimeManager, RuntimeOptions};

#[tokio::main]
async fn main() -> Result<(), Box<dyn std::error::Error>> {
    let mut args = std::env::args().skip(1);
    let cache = args.next().ok_or("expected cache directory")?;
    let archive = args.next().ok_or("expected retained official archive")?;
    let installed = RuntimeManager::new(RuntimeOptions {
        runtime_dir: Some(cache.into()),
        archive_path: (!archive.is_empty()).then(|| archive.into()),
        allow_download: Some(false),
        ..Default::default()
    })?
    .install()
    .await?;
    println!("{}", installed.path.display());
    Ok(())
}

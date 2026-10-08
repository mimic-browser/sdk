use mimic_sdk::{chromiumoxide::Session, RuntimeOptions};

#[tokio::main]
async fn main() -> Result<(), Box<dyn std::error::Error>> {
    let mut session = Session::launch(RuntimeOptions::default()).await?;
    let page = session.browser.new_page("about:blank").await?;
    page.goto("https://example.com").await?;
    let title: String = page.evaluate("document.title").await?.into_value()?;
    println!("{title}");
    page.close().await?;
    session.close().await?;
    Ok(())
}

//! `worker` binary — one supervisord-managed pipeline stage process.
//!
//! Mirrors the Python entry point's CLI so `supervisord.conf` needs no change:
//!
//! ```text
//! worker --stage {normalize|ocr|llm|postprocess} [--name worker-ab12cd34]
//! ```

use std::process::ExitCode;

const STAGES: [&str; 4] = ["normalize", "ocr", "llm", "postprocess"];

struct Args {
    stage: String,
    name: String,
}

fn parse_args() -> Result<Args, String> {
    let mut stage: Option<String> = None;
    let mut name: Option<String> = None;
    let mut argv = std::env::args().skip(1);

    while let Some(arg) = argv.next() {
        match arg.as_str() {
            "--stage" => {
                stage = Some(argv.next().ok_or("--stage requires a value")?);
            }
            "--name" => {
                name = Some(argv.next().ok_or("--name requires a value")?);
            }
            "-h" | "--help" => return Err(usage()),
            other => {
                // Support `--stage=llm` as well as `--stage llm`.
                if let Some(v) = other.strip_prefix("--stage=") {
                    stage = Some(v.to_string());
                } else if let Some(v) = other.strip_prefix("--name=") {
                    name = Some(v.to_string());
                } else {
                    return Err(format!("unrecognized argument: {other}\n{}", usage()));
                }
            }
        }
    }

    let stage = stage.ok_or_else(|| format!("--stage is required\n{}", usage()))?;
    if !STAGES.contains(&stage.as_str()) {
        return Err(format!(
            "invalid --stage '{stage}' (choose from {})",
            STAGES.join(", ")
        ));
    }
    // Python defaulted to `worker-{uuid4().hex[:8]}`.
    let name = name.unwrap_or_else(|| {
        let id = uuid::Uuid::new_v4().simple().to_string();
        format!("worker-{}", &id[..8])
    });
    Ok(Args { stage, name })
}

fn usage() -> String {
    format!("usage: worker --stage {{{}}} [--name NAME]", STAGES.join("|"))
}

#[tokio::main]
async fn main() -> ExitCode {
    augocr_common::logging::init();

    let args = match parse_args() {
        Ok(args) => args,
        Err(message) => {
            eprintln!("{message}");
            return ExitCode::FAILURE;
        }
    };

    match augocr_pipeline::run_worker(&args.stage, &args.name).await {
        Ok(()) => ExitCode::SUCCESS,
        Err(e) => {
            tracing::error!("worker stage={} exited with error: {e:#}", args.stage);
            ExitCode::FAILURE
        }
    }
}

#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

use std::sync::{
    atomic::{AtomicBool, AtomicUsize, Ordering},
    Arc, Mutex,
};

use tauri::{
    webview::{NewWindowResponse, WebviewWindowBuilder},
    Manager, RunEvent, Url, WindowEvent,
};
use tauri_plugin_dialog::{DialogExt, MessageDialogButtons, MessageDialogKind};
use tauri_plugin_shell::{
    process::{CommandChild, CommandEvent},
    ShellExt,
};

#[derive(Default)]
struct DesktopState {
    child: Mutex<Option<CommandChild>>,
    backend_origin: Mutex<Option<String>>,
    open_jobs: AtomicUsize,
    dialog_open: AtomicBool,
    shutdown_requested: AtomicBool,
    startup_failed: AtomicBool,
    allow_exit: AtomicBool,
}

fn packaged_app_url(url: &Url) -> bool {
    (url.scheme() == "tauri" && url.host_str() == Some("localhost"))
        || (matches!(url.scheme(), "http" | "https") && url.host_str() == Some("tauri.localhost"))
}

fn validated_backend_origin(url: &Url) -> Option<String> {
    if url.scheme() != "http"
        || url.host_str() != Some("127.0.0.1")
        || url.port().is_none()
        || !url.username().is_empty()
        || url.password().is_some()
        || url.path() != "/"
        || url.query().is_some()
        || url.fragment().is_some()
    {
        return None;
    }
    Some(url.origin().ascii_serialization())
}

fn external_url_allowed(url: &Url) -> bool {
    matches!(url.scheme(), "https" | "vmrc")
}

fn open_external_url(url: &Url) {
    if !external_url_allowed(url) {
        eprintln!(
            "Blocked external navigation with unsupported scheme: {}",
            url.scheme()
        );
        return;
    }
    if let Err(error) = open::that_detached(url.as_str()) {
        eprintln!(
            "Could not open external {} URL with the operating system: {error}",
            url.scheme()
        );
    }
}

fn navigation_allowed(url: &Url, state: &DesktopState) -> bool {
    if packaged_app_url(url) {
        return true;
    }
    let origin = url.origin().ascii_serialization();
    if state
        .backend_origin
        .lock()
        .is_ok_and(|expected| expected.as_deref() == Some(origin.as_str()))
    {
        return true;
    }
    open_external_url(url);
    false
}

fn kill_sidecar(state: &DesktopState) {
    if let Ok(mut guard) = state.child.lock() {
        if let Some(child) = guard.take() {
            let _ = child.kill();
        }
    }
}

fn abort_startup(app: &tauri::AppHandle, state: &DesktopState, detail: &str) {
    eprintln!("vFleet desktop startup failed: {detail}");
    state.startup_failed.store(true, Ordering::SeqCst);
    kill_sidecar(state);
    state.allow_exit.store(true, Ordering::SeqCst);
    let exit_handle = app.clone();
    app.dialog()
        .message(
            "vFleet could not start its local backend. Review the application logs and try again.",
        )
        .title("vFleet could not start")
        .kind(MessageDialogKind::Error)
        .show(move |_| exit_handle.exit(1));
}

fn write_child(state: &DesktopState, message: &str) -> Result<(), String> {
    let mut guard = state.child.lock().map_err(|_| "sidecar lock is poisoned")?;
    let child = guard.as_mut().ok_or("sidecar is not running")?;
    child
        .write(message.as_bytes())
        .map_err(|error| error.to_string())
}

fn stop_sidecar(app: &tauri::AppHandle, state: Arc<DesktopState>, force: bool) {
    if state.shutdown_requested.swap(true, Ordering::SeqCst) {
        return;
    }
    let command = if force {
        "FORCE_SHUTDOWN\n"
    } else {
        "SHUTDOWN\n"
    };
    if let Err(error) = write_child(&state, command) {
        eprintln!("Could not request sidecar shutdown: {error}");
        kill_sidecar(&state);
        state.allow_exit.store(true, Ordering::SeqCst);
        app.exit(1);
    }
}

fn request_shutdown(app: tauri::AppHandle, state: Arc<DesktopState>) {
    if state.shutdown_requested.load(Ordering::SeqCst)
        || state.dialog_open.swap(true, Ordering::SeqCst)
    {
        return;
    }
    let count = state.open_jobs.load(Ordering::SeqCst);
    if count == 0 {
        state.dialog_open.store(false, Ordering::SeqCst);
        stop_sidecar(&app, state, false);
        return;
    }

    let prompt_state = state.clone();
    let prompt_app = app.clone();
    app.dialog()
        .message(format!(
            concat!(
                "vFleet has {} queued or running job(s). Closing now interrupts local execution. ",
                "Durable jobs resume after the next launch, but an in-flight VMware task may keep ",
                "running remotely."
            ),
            count
        ))
        .title("Close vFleet with active work?")
        .kind(MessageDialogKind::Warning)
        .buttons(MessageDialogButtons::OkCancelCustom(
            "Close and resume later".into(),
            "Keep vFleet open".into(),
        ))
        .show(move |confirmed| {
            prompt_state.dialog_open.store(false, Ordering::SeqCst);
            if confirmed {
                stop_sidecar(&prompt_app, prompt_state, true);
            }
        });
}

fn main() {
    let state = Arc::new(DesktopState::default());
    let setup_state = state.clone();
    let event_state = state.clone();

    let app = tauri::Builder::default()
        .plugin(tauri_plugin_shell::init())
        .plugin(tauri_plugin_dialog::init())
        .setup(move |app| {
            let app_handle = app.handle().clone();
            let close_handle = app.handle().clone();
            let close_state = setup_state.clone();
            let window_config = app
                .config()
                .app
                .windows
                .iter()
                .find(|config| config.label == "main")
                .ok_or("main window configuration is missing")?;
            let navigation_state = setup_state.clone();
            let window = WebviewWindowBuilder::from_config(app, window_config)?
                .on_navigation(move |url| navigation_allowed(url, &navigation_state))
                .on_new_window(|url, _| {
                    open_external_url(&url);
                    NewWindowResponse::Deny
                })
                .build()?;
            window.on_window_event(move |event| {
                if let WindowEvent::CloseRequested { api, .. } = event {
                    if !close_state.allow_exit.load(Ordering::SeqCst) {
                        api.prevent_close();
                        request_shutdown(close_handle.clone(), close_state.clone());
                    }
                }
            });

            let sidecar = match app.shell().sidecar("vfleet-backend") {
                Ok(command) => command,
                Err(error) => {
                    abort_startup(
                        &app_handle,
                        &setup_state,
                        &format!("could not configure the packaged backend: {error}"),
                    );
                    return Ok(());
                }
            };
            let (mut events, child) = match sidecar.args(["--port", "0"]).spawn() {
                Ok(process) => process,
                Err(error) => {
                    abort_startup(
                        &app_handle,
                        &setup_state,
                        &format!("could not launch the packaged backend: {error}"),
                    );
                    return Ok(());
                }
            };
            *setup_state
                .child
                .lock()
                .map_err(|_| "sidecar lock is poisoned")? = Some(child);

            let output_state = setup_state.clone();
            tauri::async_runtime::spawn(async move {
                while let Some(event) = events.recv().await {
                    match event {
                        CommandEvent::Stdout(bytes) => {
                            let line = String::from_utf8_lossy(&bytes).trim().to_owned();
                            if let Some(url) = line.strip_prefix("VFLEET_READY ") {
                                match Url::parse(url).ok().and_then(|url| {
                                    validated_backend_origin(&url).map(|origin| (url, origin))
                                }) {
                                    Some((url, origin)) => {
                                        if let Ok(mut expected) = output_state.backend_origin.lock() {
                                            *expected = Some(origin);
                                        } else {
                                            abort_startup(
                                                &app_handle,
                                                &output_state,
                                                "could not store the backend origin",
                                            );
                                            break;
                                        }
                                        let navigation_error =
                                            match app_handle.get_webview_window("main") {
                                                Some(window) => window
                                                    .navigate(url)
                                                    .err()
                                                    .map(|error| error.to_string()),
                                                None => Some("main window is unavailable".into()),
                                            };
                                        if let Some(error) = navigation_error {
                                            abort_startup(
                                                &app_handle,
                                                &output_state,
                                                &format!("could not open the local UI: {error}"),
                                            );
                                            break;
                                        }
                                    }
                                    None => {
                                        abort_startup(
                                            &app_handle,
                                            &output_state,
                                            "sidecar announced an invalid backend origin",
                                        );
                                        break;
                                    }
                                }
                            } else if let Some(count) = line.strip_prefix("VFLEET_STATUS ") {
                                if let Ok(count) = count.parse::<usize>() {
                                    output_state.open_jobs.store(count, Ordering::SeqCst);
                                }
                            } else if let Some(count) = line.strip_prefix("VFLEET_BUSY ") {
                                if let Ok(count) = count.parse::<usize>() {
                                    output_state.open_jobs.store(count, Ordering::SeqCst);
                                }
                                output_state.shutdown_requested.store(false, Ordering::SeqCst);
                                request_shutdown(app_handle.clone(), output_state.clone());
                            }
                        }
                        CommandEvent::Stderr(bytes) => {
                            eprintln!("vFleet backend: {}", String::from_utf8_lossy(&bytes).trim());
                        }
                        CommandEvent::Error(error) => eprintln!("vFleet backend stream error: {error}"),
                        CommandEvent::Terminated(payload) => {
                            if let Ok(mut guard) = output_state.child.lock() {
                                *guard = None;
                            }
                            output_state.allow_exit.store(true, Ordering::SeqCst);
                            if output_state.startup_failed.load(Ordering::SeqCst) {
                                break;
                            }
                            let expected = output_state.shutdown_requested.load(Ordering::SeqCst);
                            if !expected {
                                let code = payload.code.unwrap_or(1);
                                let exit_handle = app_handle.clone();
                                app_handle
                                    .dialog()
                                    .message(format!(
                                        "The local vFleet backend stopped unexpectedly (exit code {code})."
                                    ))
                                    .title("vFleet backend stopped")
                                    .kind(MessageDialogKind::Error)
                                    .show(move |_| exit_handle.exit(code));
                            } else {
                                app_handle.exit(payload.code.unwrap_or(0));
                            }
                            break;
                        }
                        _ => {}
                    }
                }
            });
            Ok(())
        })
        .build(tauri::generate_context!())
        .expect("failed to build the vFleet desktop application");

    app.run(move |app_handle, event| {
        if let RunEvent::ExitRequested { api, .. } = event {
            if !event_state.allow_exit.load(Ordering::SeqCst) {
                api.prevent_exit();
                request_shutdown(app_handle.clone(), event_state.clone());
            }
        }
    });
}

#[cfg(test)]
mod tests {
    use super::{external_url_allowed, packaged_app_url, validated_backend_origin};
    use tauri::Url;

    #[test]
    fn accepts_only_the_packaged_app_origins() {
        assert!(packaged_app_url(
            &Url::parse("tauri://localhost/index.html").unwrap()
        ));
        assert!(packaged_app_url(
            &Url::parse("http://tauri.localhost/index.html").unwrap()
        ));
        assert!(!packaged_app_url(
            &Url::parse("https://example.test/").unwrap()
        ));
    }

    #[test]
    fn validates_exact_loopback_sidecar_origins() {
        let url = Url::parse("http://127.0.0.1:43123/").unwrap();
        assert_eq!(
            validated_backend_origin(&url).as_deref(),
            Some("http://127.0.0.1:43123")
        );

        for candidate in [
            "https://127.0.0.1:43123/",
            "http://localhost:43123/",
            "http://0.0.0.0:43123/",
            "http://127.0.0.1/",
            "http://user@127.0.0.1:43123/",
            "http://127.0.0.1:43123/other",
            "http://127.0.0.1:43123/?next=https://example.test",
        ] {
            assert_eq!(
                validated_backend_origin(&Url::parse(candidate).unwrap()),
                None
            );
        }
    }

    #[test]
    fn permits_only_expected_external_url_schemes() {
        assert!(external_url_allowed(
            &Url::parse("https://docs.example.test/vfleet").unwrap()
        ));
        assert!(external_url_allowed(
            &Url::parse("vmrc://clone:ticket@example.test/?moid=vm-42").unwrap()
        ));

        for candidate in [
            "http://example.test/",
            "file:///etc/passwd",
            "javascript:alert(1)",
            "data:text/html,unsafe",
        ] {
            assert!(!external_url_allowed(&Url::parse(candidate).unwrap()));
        }
    }
}

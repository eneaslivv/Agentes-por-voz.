// Prevents additional console window on Windows in release
#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

mod clipboard;

use tauri::{
    menu::{Menu, MenuItem},
    tray::{MouseButton, MouseButtonState, TrayIconBuilder, TrayIconEvent},
    Manager,
};

fn main() {
    tauri::Builder::default()
        .plugin(tauri_plugin_shell::init())
        .plugin(tauri_plugin_global_shortcut::Builder::new().build())
        .setup(|app| {
            // Create tray menu
            let quit_item = MenuItem::with_id(app, "quit", "Salir", true, None::<&str>)?;
            let show_item = MenuItem::with_id(app, "show", "Mostrar", true, None::<&str>)?;
            let menu = Menu::with_items(app, &[&show_item, &quit_item])?;

            // Create tray icon
            let _tray = TrayIconBuilder::new()
                .menu(&menu)
                .tooltip("Antigravity Voice")
                .on_menu_event(|app, event| match event.id.as_ref() {
                    "quit" => {
                        app.exit(0);
                    }
                    "show" => {
                        if let Some(window) = app.get_webview_window("main") {
                            let _ = window.show();
                            let _ = window.set_focus();
                        }
                    }
                    _ => {}
                })
                .on_tray_icon_event(|tray, event| {
                    if let TrayIconEvent::Click {
                        button: MouseButton::Left,
                        button_state: MouseButtonState::Up,
                        ..
                    } = event
                    {
                        let app = tray.app_handle();
                        if let Some(window) = app.get_webview_window("main") {
                            let _ = window.show();
                            let _ = window.set_focus();
                        }
                    }
                })
                .build(app)?;

            // Register global shortcut (Cmd/Ctrl + Shift + D)
            #[cfg(desktop)]
            {
                use tauri_plugin_global_shortcut::{Code, GlobalShortcutExt, Modifiers, Shortcut};

                // Ctrl+Win (Super) for dictation - matches wispr_client.py hotkey
                let shortcut = Shortcut::new(Some(Modifiers::CONTROL | Modifiers::SUPER), Code::Space);
                
                app.global_shortcut().on_shortcut(shortcut, |app, _shortcut, _event| {
                    if let Some(window) = app.get_webview_window("main") {
                        let _ = window.show();
                        let _ = window.set_focus();
                        // Emit event to start dictation mode
                        let _ = window.emit("start-dictation", ());
                    }
                })?;
            }

            Ok(())
        })
        .invoke_handler(tauri::generate_handler![
            clipboard::copy_to_clipboard,
            clipboard::get_clipboard_text,
            clipboard::simulate_paste,
            clipboard::copy_and_paste,
            clipboard::type_text,
        ])
        .run(tauri::generate_context!())
        .expect("error while running tauri application");
}


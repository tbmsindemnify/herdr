use super::*;

fn focus_count(out: &ClientShellInput) -> usize {
    out.actions
        .iter()
        .filter(|action| {
            matches!(action,
                ClientShellAction::Endpoint { request, .. }
                if matches!(request.method, crate::api::schema::Method::PaneFocus(_))
            )
        })
        .count()
}

#[test]
fn pointer_entry_focus_does_not_override_keyboard_focus_inside_same_pane() {
    let mut state = ClientShellState::new(ClientShellConfig::from_config(&Config::default()));
    state.set_snapshot(Box::new(snapshot()));
    state.set_pane_surface(surface());
    state.compose(106, 20).unwrap();
    let hit = state.hits.panes[0].clone();
    // Simulate a keyboard-selected pane elsewhere while the pointer enters here.
    state.snapshot.as_mut().unwrap().focused_pane_id = Some("pane_elsewhere".into());
    let mut mouse = MouseEvent {
        kind: MouseEventKind::Moved,
        column: hit.inner_rect.x + 1,
        row: hit.inner_rect.y,
        modifiers: KeyModifiers::empty(),
    };
    assert_eq!(
        focus_count(&state.handle_raw_events(vec![RawInputEvent::Mouse(mouse)])),
        1
    );
    // A keyboard command changes focus; pointer jitter in this pane cannot reclaim it.
    mouse.column += 1;
    assert_eq!(
        focus_count(&state.handle_raw_events(vec![RawInputEvent::Mouse(mouse)])),
        0
    );
    let mut outside = mouse;
    outside.column = 0;
    outside.row = 0;
    state.handle_raw_events(vec![RawInputEvent::Mouse(outside)]);
    assert_eq!(
        focus_count(&state.handle_raw_events(vec![RawInputEvent::Mouse(mouse)])),
        1
    );
}

#[test]
fn modifier_hover_does_not_change_keyboard_focus() {
    let mut state = ClientShellState::new(ClientShellConfig::from_config(&Config::default()));
    state.set_snapshot(Box::new(snapshot()));
    state.set_pane_surface(surface());
    state.compose(106, 20).unwrap();
    state.snapshot.as_mut().unwrap().focused_pane_id = Some("pane_elsewhere".into());
    let hit = &state.hits.panes[0];
    let mouse = MouseEvent {
        kind: MouseEventKind::Moved,
        column: hit.inner_rect.x + 1,
        row: hit.inner_rect.y,
        modifiers: KeyModifiers::ALT,
    };
    assert_eq!(
        focus_count(&state.handle_raw_events(vec![RawInputEvent::Mouse(mouse)])),
        0
    );
}

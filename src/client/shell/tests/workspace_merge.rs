use super::*;

fn merge_state() -> ClientShellState {
    let mut state = ClientShellState::new(ClientShellConfig::from_config(&Config::default()));
    let mut snap = snapshot();
    let mut workspace = snap.workspaces[0].clone();
    workspace.workspace_id = "ws_2".into();
    workspace.active_tab_id = "tab_2".into();
    workspace.number = 2;
    workspace.focused = false;
    snap.workspaces.push(workspace);
    let mut tab = snap.tabs[0].clone();
    tab.workspace_id = "ws_2".into();
    tab.tab_id = "tab_2".into();
    snap.tabs.push(tab);
    let mut pane = snap.panes[0].clone();
    pane.pane_id = "destination".into();
    pane.workspace_id = "ws_2".into();
    pane.tab_id = "tab_2".into();
    snap.panes.push(pane);
    let mut second = snap.panes[0].clone();
    second.pane_id = "source_second".into();
    snap.panes.push(second);
    state.set_snapshot(Box::new(snap));
    state.set_pane_surface(surface());
    state.compose(106, 24).unwrap();
    state
}

#[test]
fn workspace_drop_moves_existing_panes_serially_without_closing_sessions() {
    let mut state = merge_state();
    // The installed 0.9.1 server omits pane.move from its shell allowlist.
    state.set_endpoint_methods(Some(vec!["pane.focus".into(), "tab.focus".into()]));
    let from = state.hits.workspaces[0].rect;
    let to = state.hits.workspaces[1].rect;
    for (kind, rect) in [
        (MouseEventKind::Down(MouseButton::Left), from),
        (MouseEventKind::Drag(MouseButton::Left), to),
    ] {
        state.handle_raw_events(vec![RawInputEvent::Mouse(MouseEvent {
            kind,
            column: rect.x + 2,
            row: rect.y,
            modifiers: KeyModifiers::empty(),
        })]);
    }
    let out = state.handle_raw_events(vec![RawInputEvent::Mouse(MouseEvent {
        kind: MouseEventKind::Up(MouseButton::Left),
        column: to.x + 2,
        row: to.y,
        modifiers: KeyModifiers::empty(),
    })]);
    assert!(
        matches!(&out.actions[..], [ClientShellAction::StartReview { .. }, ClientShellAction::Endpoint { request, .. }]
        if matches!(&request.method, crate::api::schema::Method::PaneMove(p)
            if p.pane_id == "pane_1" && !p.focus))
    );
    assert!(state.pending_requests.values().any(|p| matches!(&p.kind,
        PendingEndpointKind::WorkspaceMerge { remaining, tab_id, .. }
        if remaining == &VecDeque::from(["source_second".to_string()]) && tab_id == "tab_2")));
    let mut duplicate = ClientShellInput::default();
    state.begin_workspace_merge("ws_1", "ws_2", &mut duplicate);
    assert!(duplicate.actions.is_empty());
    let (_, actions) = state.continue_workspace_merge(
        VecDeque::from(["source_second".into()]),
        "tab_2".into(),
        "destination".into(),
        Err(ClientShellEndpointError {
            code: Some("endpoint_timeout".into()),
            message: "timeout".into(),
        }),
    );
    assert!(
        actions.is_empty(),
        "an ambiguous move must never be retried"
    );
}

#[test]
fn workspace_merge_rejects_self_and_zoomed_destination() {
    let mut state = merge_state();
    let mut out = ClientShellInput::default();
    state.begin_workspace_merge("ws_1", "ws_1", &mut out);
    assert!(out.actions.is_empty());
    state.snapshot.as_mut().unwrap().tabs[1].zoomed = true;
    state.begin_workspace_merge("ws_1", "ws_2", &mut out);
    assert!(out.actions.is_empty());
    assert!(state.visible_endpoint_notice.is_some());
}

#[test]
fn priority_agent_drag_moves_only_that_pane_without_review_or_input() {
    let mut state = merge_state();
    let mut out = ClientShellInput::default();
    state.begin_agent_drop("pane_1", "destination", &mut out);
    assert!(matches!(&out.actions[..], [ClientShellAction::Endpoint { request, .. }]
        if matches!(&request.method, crate::api::schema::Method::PaneMove(p)
            if p.pane_id == "pane_1" && p.focus)));
    let mut out = ClientShellInput::default();
    state.begin_agent_drop("pane_1", "pane_1", &mut out);
    assert!(out.actions.is_empty());
}

#[test]
fn pane_context_menu_paste_keeps_multiline_text_on_clicked_pane() {
    let mut state = merge_state();
    state.open_pane_context_menu("destination".into(), 4, 4);
    let Some(ClientShellOverlay::ContextMenu(menu)) = state.overlay.as_ref() else { panic!("menu"); };
    assert!(menu.items().iter().any(|item| item.label == "Paste"));
    let mut out = ClientShellInput::default();
    state.paste_into_pane_with("destination".into(), &mut out, || Some("first\nsecond".into()));
    assert!(matches!(&out.requests[..], [ClientMessage::ClientShellPaneInput {pane_id, events}]
        if pane_id == "destination" && matches!(&events[..], [ClientPaneInputEvent::Paste(text)] if text == "first\nsecond")));
    assert!(out.actions.is_empty());
}

#[test]
fn priority_row_mouse_gesture_joins_panes_and_click_still_focuses() {
    let mut state = merge_state();
    let row = ratatui::layout::Rect::new(2, 19, 18, 1);
    state.hits.agents = vec![(row, "pane_1".into())];
    state.hits.endpoint_agents.clear();
    let target = state.hits.workspaces[1].rect;
    let mut result = ClientShellInput::default();
    for (kind,x,y) in [
        (MouseEventKind::Down(MouseButton::Left), row.x + 1,row.y),
        (MouseEventKind::Drag(MouseButton::Left), target.x + 1,target.y),
        (MouseEventKind::Up(MouseButton::Left), target.x + 1,target.y),
    ] { result = state.handle_raw_events(vec![RawInputEvent::Mouse(MouseEvent {kind,column:x,row:y,modifiers:KeyModifiers::empty()})]); }
    assert!(matches!(&result.actions[..], [ClientShellAction::Endpoint {request,..}]
        if matches!(&request.method, crate::api::schema::Method::PaneMove(p) if p.pane_id == "pane_1")));
    assert!(result.requests.is_empty());
    for kind in [MouseEventKind::Down(MouseButton::Left),MouseEventKind::Up(MouseButton::Left)] {
        result = state.handle_raw_events(vec![RawInputEvent::Mouse(MouseEvent {kind,column:row.x+1,row:row.y,modifiers:KeyModifiers::empty()})]);
    }
    assert!(matches!(&result.actions[..], [ClientShellAction::Endpoint {request,..}]
        if matches!(&request.method, crate::api::schema::Method::PaneFocus(p) if p.pane_id == "pane_1")));
}

#[test]
fn agent_drop_and_paste_reject_stale_targets_and_zoom() {
    let mut state = merge_state();
    let mut out = ClientShellInput::default();
    state.begin_agent_drop("pane_1", "missing", &mut out);
    state.snapshot.as_mut().unwrap().tabs[1].zoomed = true;
    state.begin_agent_drop("pane_1", "destination", &mut out);
    state.paste_into_pane_with("missing".into(), &mut out, || panic!("stale target must not read clipboard"));
    state.paste_into_pane_with("destination".into(), &mut out, || None);
    assert!(out.actions.is_empty());
    assert!(out.requests.is_empty());
}

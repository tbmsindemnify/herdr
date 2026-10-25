use super::*;
use crate::api::schema::{
    Method, PaneMoveDestination, PaneMoveParams, ResponseResult, SplitDirection,
};

impl ClientShellState {
    pub(super) fn add_tiled_pane(&mut self, outcome: &mut ClientShellInput) {
        let Some(pane_id) = self.focused_pane_id() else { return; };
        let rect = self.hits.panes.iter().find(|p| p.pane_id == pane_id)
            .map(|p| p.inner_rect);
        let direction = if rect.is_none_or(|r| r.width > r.height.saturating_mul(2)) {
            SplitDirection::Right
        } else {
            SplitDirection::Down
        };
        self.push_endpoint_method(Method::PaneSplit(crate::api::schema::PaneSplitParams {
            workspace_id: None,
            target_pane_id: Some(pane_id),
            direction,
            ratio: Some(0.5),
            cwd: None,
            focus: true,
            right_click: Default::default(),
            env: Default::default(),
        }), outcome);
    }

    // This is client-side orchestration of existing public pane.move operations.
    // No protocol change or server restart is needed; each move preserves its PTY.
    pub(super) fn begin_workspace_merge(
        &mut self,
        source: &str,
        destination: &str,
        outcome: &mut ClientShellInput,
    ) {
        self.begin_review_merge(source, destination, None, outcome);
    }

    pub(super) fn begin_tab_merge(&mut self, source: &str, destination: &str, tab_id: &str, outcome: &mut ClientShellInput) {
        self.begin_review_merge(source, destination, Some(tab_id), outcome);
    }

    fn begin_review_merge(&mut self, source: &str, destination: &str, source_tab: Option<&str>, outcome: &mut ClientShellInput) {
        if source == destination
            || self
                .pending_requests
                .values()
                .any(|p| matches!(p.kind, PendingEndpointKind::WorkspaceMerge { .. }))
        {
            return;
        }
        let Some(snapshot) = self.snapshot.as_deref() else {
            return;
        };
        let Some(from) = snapshot
            .workspaces
            .iter()
            .find(|w| w.workspace_id == source)
        else {
            return;
        };
        let Some(to) = snapshot
            .workspaces
            .iter()
            .find(|w| w.workspace_id == destination)
        else {
            return;
        };
        // Linked worktree ownership has semantics beyond terminal layout.
        if from.worktree.is_some() || to.worktree.is_some() {
            outcome.repaint |= self.push_endpoint_notice(
                ClientEndpointNoticeKind::Rejected,
                "merge_worktree",
                "Cannot combine linked workspaces",
                "Move individual panes instead; worktree ownership remains unchanged.",
            );
            return;
        }
        let tab_id = to.active_tab_id.clone();
        if snapshot.tabs.iter().any(|t| t.tab_id == tab_id && t.zoomed) {
            outcome.repaint |= self.push_endpoint_notice(
                ClientEndpointNoticeKind::Rejected,
                "merge_zoomed",
                "Unzoom the destination first",
                "No panes were moved. Unzoom its tab, then drag again.",
            );
            return;
        }
        let Some(target) = snapshot
            .panes
            .iter()
            .find(|p| p.tab_id == tab_id)
            .map(|p| p.pane_id.clone())
        else {
            return;
        };
        let remaining: VecDeque<String> = snapshot
            .panes
            .iter()
            .filter(|p| p.workspace_id == source && source_tab.is_none_or(|tab| p.tab_id == tab))
            .map(|p| p.pane_id.clone())
            .collect();
        if self.active_endpoint_id.is_local() {
            outcome.actions.push(ClientShellAction::StartReview {
                sources: remaining.iter().cloned().collect(), reviewer: target.clone(),
            });
        }
        self.queue_workspace_merge(remaining, tab_id, target.clone(), target, SplitDirection::Right, 0.72, outcome);
    }

    fn queue_workspace_merge(
        &mut self,
        mut remaining: VecDeque<String>,
        tab_id: String,
        target: String,
        reviewer: String,
        split: SplitDirection,
        ratio: f32,
        outcome: &mut ClientShellInput,
    ) {
        let Some(pane_id) = remaining.pop_front() else {
            return;
        };
        self.push_endpoint_method_with_kind(
            Method::PaneMove(PaneMoveParams {
                pane_id,
                destination: PaneMoveDestination::Tab {
                    tab_id: tab_id.clone(),
                    target_pane_id: Some(target),
                    split,
                    ratio: Some(ratio),
                },
                focus: false,
            }),
            PendingEndpointKind::WorkspaceMerge { remaining, tab_id, reviewer },
            outcome,
        );
    }

    pub(super) fn continue_workspace_merge(
        &mut self,
        remaining: VecDeque<String>,
        tab_id: String,
        reviewer: String,
        result: Result<ResponseResult, ClientShellEndpointError>,
    ) -> (bool, Vec<ClientShellAction>) {
        // A rejection or timeout stops the chain; never retry an ambiguous move.
        let Ok(ResponseResult::PaneMove { move_result }) = result else {
            return (true, Vec::new());
        };
        if !move_result.changed {
            self.push_endpoint_notice(
                ClientEndpointNoticeKind::Rejected,
                "merge_unchanged",
                "Combination stopped",
                "The server could not move a pane. All sessions remain running.",
            );
            return (true, Vec::new());
        }
        let mut outcome = ClientShellInput::default();
        if remaining.is_empty() {
            self.push_endpoint_method(
                Method::TabFocus(crate::api::schema::TabTarget { tab_id }),
                &mut outcome,
            );
        } else if let Some(target) = move_result
            .target_layout
            .panes
            .iter()
            .filter(|p| p.pane_id != reviewer)
            .max_by_key(|p| u32::from(p.rect.width) * u32::from(p.rect.height))
        {
            // Terminal cells are approximately twice as tall as they are wide.
            let split = if target.rect.width > target.rect.height.saturating_mul(2) {
                SplitDirection::Right
            } else {
                SplitDirection::Down
            };
            self.queue_workspace_merge(
                remaining,
                tab_id,
                target.pane_id.clone(),
                reviewer,
                split,
                0.5,
                &mut outcome,
            );
        }
        (true, outcome.actions)
    }
}

impl ClientShellState {
    // Move exactly the dragged agent; never start a review or submit terminal input.
    pub(super) fn begin_agent_drop(&mut self, source: &str, target: &str, out: &mut ClientShellInput) {
        if source == target { return; }
        let Some(s) = self.snapshot.as_deref() else { return; };
        if !s.panes.iter().any(|p| p.pane_id == source) { return; }
        let Some(p) = s.panes.iter().find(|p| p.pane_id == target) else { return; };
        if s.tabs.iter().any(|t| t.tab_id == p.tab_id && t.zoomed) {
            out.repaint |= self.push_endpoint_notice(ClientEndpointNoticeKind::Rejected,
                "agent_drop_zoomed", "Unzoom the destination first", "No panes were moved.");
            return;
        }
        self.push_endpoint_method(Method::PaneMove(PaneMoveParams {
            pane_id: source.into(), destination: PaneMoveDestination::Tab {
                tab_id: p.tab_id.clone(), target_pane_id: Some(target.into()),
                split: SplitDirection::Right, ratio: Some(0.5),
            }, focus: true,
        }), out);
    }

    pub(super) fn agent_drop_target(&self, point: (u16, u16)) -> Option<String> {
        self.hits.endpoint_agents.iter().find(|(r,e,_)| *e == self.active_endpoint_id && super::contains(*r, point)).map(|(_,_,p)| p.clone())
            .or_else(|| self.hits.agents.iter().find(|(r,_)| super::contains(*r,point)).map(|(_,p)| p.clone()))
            .or_else(|| self.hits.panes.iter().find(|h| super::contains(h.rect,point)).map(|h| h.pane_id.clone()))
            .or_else(|| {
                let ws = self.active_endpoint_workspace_at(point)?;
                let s = self.snapshot.as_deref()?;
                let tab = &s.workspaces.iter().find(|w| w.workspace_id == ws)?.active_tab_id;
                s.panes.iter().find(|p| &p.tab_id == tab).map(|p| p.pane_id.clone())
            })
    }
}

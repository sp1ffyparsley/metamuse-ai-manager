---
name: Muse Monitor Windows
description: Native WPF system-control HUD; Windows client preview only.
colors:
  hud-background: "#F5F6F8"
  foreground: "#000000"
typography:
  title:
    fontFamily: "Segoe UI"
    fontSize: "22px"
    fontWeight: 600
  body:
    fontFamily: "Segoe UI"
    fontSize: "13px"
  detail:
    fontFamily: "Segoe UI"
    fontSize: "12px"
spacing:
  window-inset: "18px"
  action-horizontal: "9px"
  action-vertical: "5px"
components:
  action:
    padding: "5px 9px"
---

# Design System: Muse Monitor Windows

## Overview

Windows client preview only. The implemented surface is a small, readable WPF HUD using native system controls. Source authority is `windows/Program.cs`; the reviewed offline and populated synthetic WPF captures establish its current appearance, not deployed controller integration.

## Colors

The pale gray HUD background and black foreground use the frontmatter tokens. Buttons, fields, disabled states, borders, and selection colors come from Windows/WPF system styles; do not freeze their screenshot colors into application tokens.

## Typography

Segoe UI supplies the title, body, and detail roles above. The main title is semibold. Status and action labels remain body-sized; observations and workspace rows use the detail role. Text blocks wrap. Values use WPF device-independent units; the portable frontmatter expresses their numeric equivalents in px.

## Layout

The default window is 420 by 470 units, with a 380 by 380 minimum. Saved expanded mode starts at 780 by 650. The root inset uses the spacing token. A top dock holds heading, status, bounded workspace rows, selectors, and wrapping action controls; the live viewer has its own vertical scroll area. Workspace rows scroll within an 88-unit maximum height. Keep selectors and ownership actions reachable with populated status.

## Elevation & Depth

Application content adds no custom shadows or visual elevation. Native window and control chrome provide depth and focus treatment.

## Shapes

Use the existing WPF Button, ComboBox, CheckBox, TextBox, PasswordBox, and ScrollViewer forms. No application radius or custom border token is defined.

## Components

Actions share native buttons, frontmatter padding, a 30-unit minimum height, and margins of 0/3/6/3 units (left/top/right/bottom). Controls retain system hover, keyboard focus, and disabled appearance. Workspace and page selectors identify the exact target. The viewer uses a uniformly fitted image with adjacent textual authority state; stale/offline telemetry clears the frame. Settings is an owned native dialog. Tray reopening is explicit; ordinary close hides the HUD and clears viewing.

## Do's and Don'ts

- Do preserve system control affordances and the readable Segoe UI hierarchy.
- Do show offline, stale, and human-control state in text.
- Do keep long workspace observations inside their bounded scroll viewport.
- Don't infer current live viewing from an old frame.
- Don't replace this native surface with a web renderer or invented decorative component system.

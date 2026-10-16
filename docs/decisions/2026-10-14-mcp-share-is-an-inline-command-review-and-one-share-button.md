# Sharing an MCP server is an inline command review and one share button, with no review panel or confirm dialog

Decided by: Ray Xu (@buluoray)
Date: 2026-10-14

## Decision

On the MCP page a server is shared by pressing its "Share this server" button, with the command, args and declared env it will run shown beside that button; the separate launch review panel and the confirmation dialog on the sharing switch are gone, and a launch that changes after it was shared still pauses until the person presses "Use new command".

## Why

- Recorded on the pull request: "Maintainer accepts replacing the review panel and confirmation dialog with the inline command review and explicit share action. The intended scope includes the mcp.share chat card; no separate RFC is required."

## Evidence

- https://github.com/kirodotdev/KiroCrew/pull/19661#issuecomment-6172321245 -- the decision, recorded on the pull request that makes the change.
- https://github.com/kirodotdev/KiroCrew/pull/19661 -- the pull request.

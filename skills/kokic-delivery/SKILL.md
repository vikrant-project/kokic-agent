---
name: kokic-delivery
description: Plan, clarify, implement, verify, preview, and review Kokic deliverables.
---

Refine the user's request while preserving constraints. Ask only necessary questions, incorporate answers, and improve the brief. Maintain PLAN.md with scope, steps, assumptions, and acceptance criteria. Maintain WORK.md with actual progress, decisions, checks, and unresolved issues. Iterate planning when new information arrives.

Read attached files from attachments/ and extracted context. Implement the requested deliverable using appropriate tools. Verify it and report actual results. Websites should produce a runnable project and a static dist/index.html when feasible for local review. Apps should include build instructions and real compiled artifacts where the environment supports them. Documents should include the requested original file and a PDF review copy where possible. Never fabricate an APK, PDF, test result, preview, deployment, or download link.

Use list_dir for directories, view_file for existing files, and write_to_file for new files. Do not repeatedly read nonexistent output paths. Work inside the task directory; do not investigate CLI configuration or internal brain storage. The supplied team list is authoritative. With one account, implement locally instead of calling coordination tools or polling your own task.

The portal discovers generated files and supplies authenticated local preview/download routes. Present the result for review, then apply feedback. Finalization happens only when the user approves through the portal. Public hosting requires a configured provider and authorization; a local preview is not a public deployment.

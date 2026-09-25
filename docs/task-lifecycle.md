# Task lifecycle and recovery

An authorized issue or `/banana start` requests work. The controller verifies the configured model credential and target access, creates a branch from the configured base, and acknowledges the task on the issue. Issue text supplies the initial request; later guidance must arrive through an authorized command. Other comments are not appended to the model's instructions.

One runner holds an expiring lease stored in the issue repository's state branch. State changes use optimistic content revisions, so overlapping workflows cannot both claim the task. The runner renews its lease, polls commands, and posts progress at the configured interval, normally every two minutes.

The controller restores the task branch into an isolated workspace, installs the configured dependencies, and runs a bounded completion loop. At each iteration it saves a coherent checkpoint to the branch. The engine must declare completion, continuation, or a specific human blocker. Completion requires source changes, all acceptance checks, a clean diff-format check, and a stable source tree. The container is stopped before publication; the controller rechecks the branch and validated tree.

On success, a separate session summarizes only the proposed public diff. The controller opens an idempotent draft/WIP PR, posts its link to the issue, and closes the issue. A maintainer reviews and merges or closes the PR and may delete its branch. An issue marked complete means a contribution is ready for review; it does not mean production has been updated.

| State | Meaning | Next action |
| --- | --- | --- |
| Running | A leased runner is implementing or checking the task. | Wait, inspect status, or give an explicit command. |
| Blocked | The engine needs human guidance, or the proposed result has no source changes. | `/banana answer TEXT` resumes with guidance. |
| Paused | The time/iteration allowance ended or a recoverable interruption occurred. | `/banana resume` continues the saved branch. |
| Interrupted | The runner stopped renewing its lease. | Inspect the workflow, then resume. |
| Stopped | A maintainer requested cancellation. | Leave stopped, resume, or restart. |
| Failed | Preparation, engine, access, publication, or an explicit fail command ended the run. | Correct the cause, then resume or restart. |
| Complete | Checked changes have a PR linked on the source issue. | Human review and merge/close. |

`/banana stop` stops at the next controller polling point and attempts a final checkpoint after terminating the engine. Docker/API operations have bounded timeouts; cancellation is not instantaneous. It keeps the issue open and retains work. `/banana fail REASON` records an explicit failure. Workflow cancellation, runner loss, or a hard timeout may prevent a final checkpoint; resume restores the last successfully pushed checkpoint, not unsaved container files.

`/banana resume` keeps the branch and reruns the configured preparation and acceptance checks. A new engine session reads the original issue and saved maintainer guidance. `/banana model ALIAS` during a run checkpoints and switches sessions when the controller observes it. `/banana restart` preserves the old branch and starts a new generation from the base branch. It does not force-push away previous work.

If publication fails after a PR is created, its URL and branch remain recoverable. A retry finds the existing open PR. A closed or merged PR is not silently reused; restart to prepare a new contribution. If someone changes the working branch during final publication, the controller stops and asks for review/resume.

A model saying the task is complete does not show that every requirement is met. Configure checks that test the requested behavior and review the resulting code. BananaVibe never treats a plan, an exhausted budget, failed checks, or a request for help as successful completion.

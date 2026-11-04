# Changelog

Versions of ibmicli. The VS Code extension has the same version as the CLI,
and is updated with `ibmicli vscode install`.

## 0.2.0 - 2026-11-04

### Added

- **Check a source without deploying it.** A task can now compile the local
  source in QTEMP with the real compiler: the deployed member and your object
  library are not touched, and the module disappears when the connection
  closes. Errors appear on the lines of the local file, in the terminal and in
  the VS Code Problems panel.

  This is not a built-in command: it is a task you add to your `config.toml`
  or `ibmiproj.toml`, so you choose the library (QTEMP or a sandbox), the
  compile command and its options. The README has a complete example
  ([Checking a source without deploying it](README.md#checking-a-source-without-deploying-it)),
  and so does `ibmicli tasks --guide`. Once added, it appears in the F6 menu.

- **Steps that may fail: `job?:`, `cl?:`, `sh?:`.** A `?` before the colon
  lets the task go on when that step fails, for steps whose failure means
  "already done", such as `CRTSRCPF` of a file that already exists.

- **`cl:` prefix**, the same as an unprefixed CL step, written explicitly. It
  exists so that CL can be written as `cl?:` too.

### Changed

- **Compiler errors are read where the compiler writes them.** For a `CRT...`
  command, the event file is read from the library and name in its `OBJ()`,
  `PGM()`, `SRVPGM()`, `MODULE()` or `FILE()`, instead of always from the
  task's object library. Other commands, such as a `CALL` to your own build
  program, work as before.
- **Errors after a `job:` step.** A failed compilation in a `job:` step now
  shows its errors from the event file; before, it only said that it failed.
- The task heading shows `-> LIBRARY` only when a step uses `{objlib}`.
- The task guide explains what `job:` does and when it is needed: QTEMP is
  per job, and a normal CL step runs in a child job with its own QTEMP.

Existing tasks work as before.

## 0.1.0 - 2026-10-26

First published version.

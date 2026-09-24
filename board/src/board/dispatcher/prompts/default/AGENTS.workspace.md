# Strikebox worker runbook

Standing guidance for every worker session in this project workspace. It applies
in addition to the task prompt; when they conflict, the task prompt wins.

## Tooling first, reimplementation last
- Before writing a custom script for a known technique, check whether a
  battle-tested tool already does it and is installed on this host: the impacket
  suite (impacket-dpapi for DPAPI masterkeys/credentials/blobs,
  impacket-secretsdump, impacket-ntlmrelayx, impacket-psexec/smbexec/wmiexec,
  GetUserSPNs/GetNPUsers, rpcclient/smbclient), netexec (auth validation,
  shares, sprays), certipy (AD CS abuse), Responder, hashcat/john,
  bloodhound-python, evil-winrm, and .NET decompilers (ilspycmd) for reversing.
- Run `<tool> -h` or `which <tool>` before assuming it is missing.
- Hand-roll only when no installed tool covers the step. Any hand-rolled crypto
  or parser MUST be validated against a known-answer test (reproduce a value you
  already know) before its output is trusted.
- For DPAPI specifically, do not hand-roll the crypto: use impacket-dpapi
  `masterkey` (with -password/-sid), then `credential`/`blob` with the decrypted
  key.

## Offbox-first analysis
- When code execution yields secrets (blobs, registry hives, databases, DLLs,
  config), exfiltrate the artifacts and analyze them offline on this host
  instead of running decrypt/parse logic on the target. On-box logic multiplies
  failure modes (output truncation, AV, timeouts) and burns the access channel.
- Prefer one clean exfil of the artifact over many on-box round trips; retrieve
  large outputs in chunks only when the channel forces it.

## Channels are flaky — normalize for it
- Any remote-execution channel that has shown dropped or truncated output
  deserves: unique per-request markers, command output written to a file then
  read back, and a bounded retry policy. Never infer "command failed" from "no
  output returned" without checking a side channel (file existence, log line,
  changed timestamp).

## Sessions are short
- The task window is bounded. Prefer completing one leg end-to-end over staging
  three legs half-way. If staging is unavoidable, leave the exact resume state
  (files, offsets, commands, armed IDs) in $BOARD_EVIDENCE_DIR and name it in
  the concluding fact so the next session can pick it up in one step.

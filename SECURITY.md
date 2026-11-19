# Security policy

webPaperWriting operates on explicitly configured research projects. The project gateway delegates to a trusted local runtime, and controlled workers use the Linux isolation backend. Read the project's actual tool permissions and source scopes before connecting an AI client.

MCP results can transmit selected source text, metadata, or artifacts to the connected AI host. A local project directory can therefore still supply data to a remote service through an authorized tool call. Users control the host, project roots, credentials, and delivery settings.

Keep authentication tokens, OAuth grants, Runner registrations, cloud plugin snapshots, and private instance configuration in a user-owned private location outside Git. Share synthetic reproductions and reviewed diagnostic excerpts in public reports. Use the release checksum file to verify downloaded assets.

Report suspected vulnerabilities through this repository's **Security → Report a vulnerability** entry when available. Include the affected release, minimal synthetic reproduction, impact, and relevant configuration with secrets removed. If private reporting is unavailable, open a public issue requesting a private reporting channel and keep sensitive details out of that issue.

Security maintenance targets the latest published release. Older versions can be considered when a report supplies a reproducible affected contract. Optional renderers, Office automation, WebCodex, TeX, and cloud providers have their own security and licensing policies. [WebCodex security](https://github.com/yyjeqhc/webcodex/blob/main/SECURITY.md)

# Skills

Workspace Skill management, including the list, file editing, publishing, and version restoration.

Routes enter through `page.tsx` for the list and `detail-page.tsx` for a Skill's detail. Navigation uses `permissions.ts` for visibility; Agent V2 consumes the shared Skill error handling from `error.ts`.

File editing, draft coordination, publishing, and version UI remain internal to `detail/`.

## Import from GitHub

Choose **Import → GitHub** and paste a public repository URL or a directory URL:

- `https://github.com/owner/repo` imports the default branch's root Skill.
- `https://github.com/owner/repo/tree/ref/path/to/skill` imports that directory. Branches, tags, and commit SHAs are supported. For refs containing `/`, the longest existing ref wins.

The selected root must contain `SKILL.md`; repositories containing multiple Skills need a URL to an individual Skill directory. Import creates a new draft and opens its editor. Publishing remains a separate action. Existing names are not overwritten.

Imports are snapshots with no automatic synchronization or private repository access. The backend resolves the ref to a commit before downloading. GitHub metadata requests and archive downloads follow the SSRF policy without credentials or redirects. The complete repository archive must fit `UPLOAD_SKILL_FILE_SIZE_LIMIT`, `SKILL_PACKAGE_MAX_ENTRIES`, and `SKILL_PACKAGE_MAX_UNCOMPRESSED_BYTES`; large repositories may need a local ZIP import instead. GitHub's unauthenticated API rate limit also applies.

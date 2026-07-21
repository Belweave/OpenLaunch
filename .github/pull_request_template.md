<!--
⚠️ CRITICAL CHECKS FOR CONTRIBUTORS (READ, DON'T DELETE) ⚠️
1. Target the `dev` branch. PRs targeting `main` will be automatically closed.
2. Do NOT delete the CLA section at the bottom. It is required for the bot to accept your PR.
-->

# Pull Request Checklist

### Note to first-time contributors: Please open a discussion post in [Discussions](https://github.com/belweave/openlaunch/discussions) to discuss your idea/fix with the community before creating a pull request, and describe your changes before submitting a pull request.

This is to ensure large feature PRs are discussed with the community first, before starting work on it. If the community does not want this feature or it is not relevant for OpenLaunch as a project, it can be identified in the discussion before working on the feature and submitting the PR.

<!--
### ⚠️ Important: Your PR is a contribution, not a guarantee of merge.

The most impactful way to contribute to OpenLaunch is through well-written bug reports, detailed feature discussions, and thoughtful ideas. These directly shape the project. If you do open a pull request, please know that OpenLaunch is held to the highest standard of code quality, consistency, and architectural coherence, and every line merged becomes something the core team must own, maintain, and support indefinitely. Submitted code may be refactored, rewritten, or used as inspiration for a different implementation. This is not a reflection of your work's quality. It is how we ensure that a small team can deeply understand and evolve every part of the codebase.
-->

**Before submitting, make sure you've checked the following:**

- [ ] **Linked Issue/Discussion:** This PR references an existing [Issue](https://github.com/belweave/openlaunch/issues) or [Discussion](https://github.com/belweave/openlaunch/discussions) — `Closes #___` / `Relates to #___`. If one does not exist, create one first. PRs without a linked issue or discussion may be closed without review.
- [ ] **Target branch:** The pull request targets the `dev` branch. **PRs targeting `main` will be immediately closed.**
- [ ] **Description:** A concise description of the changes is provided below.
- [ ] **Changelog:** A changelog entry following [Keep a Changelog](https://keepachangelog.com/) format is included at the bottom.
- [ ] **Documentation:** Relevant documentation has been added or updated in the [OpenLaunch Docs Repository](https://github.com/belweave/openlaunch).
- [ ] **Dependencies:** Any new or updated dependencies are explained, tested, and documented.
- [ ] **Testing:** Manual tests have been performed to verify the fix/feature works correctly and does not introduce regressions. Screenshots or recordings are included where applicable.
- [ ] **No Unchecked AI Code:** This PR is either human-written or has undergone thorough human review AND manual testing. Unreviewed AI-generated PRs may be closed immediately.
- [ ] **Self-Review:** A self-review of the code has been performed, ensuring adherence to project coding standards.
- [ ] **Architecture:** Smart defaults are preferred over new settings. Local state is used for ephemeral UI logic. Major architectural or UX changes have been discussed first.
- [ ] **Git Hygiene:** The PR is atomic (one logical change), rebased on `dev`, and contains no unrelated commits.
- [ ] **Title Prefix:** The PR title uses one of the following prefixes:
  - **BREAKING CHANGE**: Changes affecting backward compatibility
  - **build**: Build system or dependency changes
  - **ci**: CI/CD workflow changes
  - **chore**: Refactoring, cleanup, or non-functional changes
  - **docs**: Documentation additions or updates
  - **feat**: New features or enhancements
  - **fix**: Bug fixes or corrections
  - **i18n**: Internationalization or localization changes
  - **perf**: Performance improvements
  - **refactor**: Code restructuring
  - **style**: Formatting changes (whitespace, semicolons, etc.)
  - **test**: Test additions or corrections
  - **WIP**: Work in progress

# Changelog Entry

### Description

- [Describe the changes, including motivation and impact]

### Added

- [New features, functionalities, or additions]

### Changed

- [Changes, updates, refactorings, or optimizations]

### Deprecated

- [Deprecated functionality or features]

### Removed

- [Removed features, files, or functionalities]

### Fixed

- [Bug fixes or corrections]

### Security

- [Security-related changes or vulnerability fixes]

### Breaking Changes

- **BREAKING CHANGE**: [Changes affecting compatibility or functionality]

---

### Additional Information

- [Any additional context, notes, or references to related issues/commits]

### Screenshots or Videos

- [Attach relevant screenshots or videos demonstrating the changes]

### License

- [ ] I confirm that my contribution may be distributed under the repository's MIT License.

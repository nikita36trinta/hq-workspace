.PHONY: install sync-skills

# Clone every submodule listed in .gitmodules.
# Submodules the current user cannot read fail silently and stay empty —
# the rest still get initialized. Run after a fresh clone and any time a
# new submodule is added.
install:
	@git config --file .gitmodules --get-regexp '^submodule\..*\.path$$' \
		| awk '{print $$2}' \
		| while read -r path; do \
			printf '→ %s\n' "$$path"; \
			if git submodule update --init --recursive "$$path" >/dev/null 2>&1; then \
				printf '  ok\n'; \
			else \
				printf '  skipped (no access)\n'; \
			fi; \
		done
	@$(MAKE) --no-print-directory sync-skills

# Aggregate component-owned skills into the workspace skill set.
#
# A component ships its own skills at .hq/<component>/.agents/skills/<skill>/SKILL.md.
# Claude Code only discovers skills ONE level deep (.claude/skills/<skill>/SKILL.md) and
# ignores any grouping subdirectory, so we can't just symlink a whole component skills/
# tree under a namespace dir — it would be invisible. Instead we surface each component
# skill as its own flat, prefixed entry: .agents/skills/<component>-<skill> -> the source.
# (.claude/skills is itself a symlink to .agents/skills, and Claude follows symlinks.)
#
# Re-runnable: every generated symlink is rebuilt from scratch; hand-authored skills
# (real directories under .agents/skills/) are never touched. The knowledge base is
# excluded — its skills run from inside the KB (relative paths to its plugins/documents)
# and are surfaced via the workspace's own rigid-* router skills instead.
SKILLS_DIR := .agents/skills
SKILLS_EXCLUDE := knowledge

sync-skills:
	@mkdir -p $(SKILLS_DIR)
	@find $(SKILLS_DIR) -maxdepth 1 -type l -delete
	@for comp_skills in .hq/*/.agents/skills; do \
		[ -d "$$comp_skills" ] || continue; \
		comp=$$(basename "$$(dirname "$$(dirname "$$comp_skills")")"); \
		case " $(SKILLS_EXCLUDE) " in *" $$comp "*) printf '  skip %s (excluded)\n' "$$comp"; continue;; esac; \
		for skill in "$$comp_skills"/*/; do \
			[ -f "$$skill/SKILL.md" ] || continue; \
			name=$$(basename "$$skill"); \
			link="$(SKILLS_DIR)/$$comp-$$name"; \
			if [ -e "$$link" ] && [ ! -L "$$link" ]; then \
				printf '  !! %s exists and is not a symlink (hand-authored?) — skipping\n' "$$link"; \
				continue; \
			fi; \
			ln -sfn "../../$$comp_skills/$$name" "$$link"; \
			printf '  → %s\n' "$$comp-$$name"; \
		done; \
	done

# Triage Labels

The skills speak in terms of five canonical triage roles. This file maps those roles to the actual label strings used in this repo's issue tracker.

| Label in mattpocock/skills | Label in our tracker | Meaning                                  |
| -------------------------- | -------------------- | ---------------------------------------- |
| `needs-triage`             | `needs-triage`       | Maintainer needs to evaluate this issue  |
| `needs-info`               | `needs-info`         | Waiting on reporter for more information |
| `ready-for-agent`          | `ready-for-agent`    | Fully specified, ready for an AFK agent  |
| `ready-for-human`          | `ready-for-human`    | Requires human implementation            |
| `wontfix`                  | `wontfix`            | Will not be actioned                     |

When a skill mentions a role (e.g. "apply the AFK-ready triage label"), use the corresponding label string from this table.

Edit the "Label in our tracker" column to match whatever vocabulary you actually use.

## Additional label: `agent-assisted`

| Label            | Meaning |
| ---------------- | ------- |
| `agent-assisted` | A human drives the implementation and an agent pairs with them (explains, reviews, writes tests and boilerplate). |

Always paired with `ready-for-human`, so unattended agents never pick these issues up. Used for the LangGraph agent, compaction, taste memory, and eval work, because learning agent engineering is a project goal.

When working an `agent-assisted` issue: don't implement it end to end. Propose the approach, let the human write the core logic, and help with review, tests, and the surrounding plumbing.

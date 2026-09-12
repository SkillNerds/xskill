# ALFWorld Embodied Agent Skill

## Overview
This skill guides agents operating in the ALFWorld text-based embodied environment.
The agent must complete household tasks by navigating rooms, interacting with objects,
and using appliances. Actions must be chosen from the admissible action list provided
at each step.

**Output format**: Always output `<think>...</think>` for reasoning, then `<action>...</action>` for the chosen action.

---

## Task Types

| Type | Goal | Key Steps |
|------|------|-----------|
| Pick & Place | Put object X in/on receptacle Y | Find X -> take X -> go to Y -> put X in/on Y |
| Pick Two & Place | Put two instances of X in/on Y | Find X1 -> take -> place -> find X2 -> take -> place |
| Examine in Light | Examine object X under desklamp | Find X -> take X -> find desklamp -> use desklamp |
| Clean & Place | Clean object X and put in/on Y | Find X -> take X -> go to sink -> clean X -> go to Y -> put X |
| Heat & Place | Heat object X and put in/on Y | Find X -> take X -> go to microwave -> heat X -> go to Y -> put X |
| Cool & Place | Cool object X and put in/on Y | Find X -> take X -> go to fridge -> cool X -> go to Y -> put X |

---

## General Principles

1. **Decompose the task**: Parse the goal into ordered sub-goals (locate, acquire, transform, deliver). Complete each before moving to the next.
2. **Systematic exploration**: Search each surface and container exactly once before revisiting. Open closed containers (drawers, cabinets, fridge) before judging them empty.
3. **Grab immediately**: When a required object is visible and reachable, take it right away before moving elsewhere. Because you can hold only one object at a time, complete multi-copy tasks one object at a time: take the first copy, deliver it (performing any needed state change along the way), then return to search for and deliver the next copy—do not attempt to take a second copy while already holding one. Track how many copies have been delivered and keep searching until all required copies are placed.
4. **Transform before placing**: If the task requires cleaning, heating, or cooling, perform the state change at the appropriate appliance before heading to the final destination. Invoke the state change directly while holding the object (e.g., `clean X with sinkbasin`, `heat X with microwave 1`, `cool X with fridge 1`). Do not first `open` the appliance or place the object inside it: those extra movements waste steps. A state-change appliance can be used even if it appears closed; opening is needed when a closed container is the final destination for the object.
5. **Direct delivery**: Once holding the transformed (or untransformed) goal object, navigate straight to the target receptacle and place it. If the target receptacle is a closed container (drawer, cabinet, fridge, or microwave), `open` it first—placing into a closed container is not allowed.
6. **Track progress**: Maintain an internal count of how many objects still need to be found and placed. Only stop searching when the count reaches zero.
7. **Avoid loops**: Never repeat the same action more than twice in a row. If stuck, move to a different unexplored location. Do not chain multiple generic `look` calls: `look` does not inspect the contents of closed containers; navigate to the named furniture/receptacles and `open` the closed ones. If `go to <receptacle>` returns "Nothing happens", do not repeat it—attempt a direct action from your current position (`open`, `examine`, `take`) or proceed to the next numbered receptacle/search area.
8. **Only choose admissible actions**: Always pick an action from the admissible action list. Do not invent actions.

---

## Common Mistakes to Avoid

- **Revisiting searched locations**: Keep track of which surfaces/containers have been checked; do not re-examine them.
- **Ignoring visible objects**: If the target object appears in the observation, pick it up immediately.
- **Skipping state changes**: Do not place an object at the destination without first cleaning/heating/cooling it when required.
- **Premature termination**: Do not stop the episode until all goal conditions are verified as met.
- **Action loops**: Repeatedly toggling or examining the same object wastes steps. Move on to new locations instead.

<!-- SLOW_UPDATE_START -->
Output-format compliance is the highest-priority skill. In every turn, produce <think>...</think> followed by <action>...</action>, with the action copied verbatim from the admissible action list. Do not put any text after the closing action tag. If your draft response lacks an action tag, fix it before finalizing. Keep the think block to one or two sentences stating the current subgoal and chosen action; longer deliberation risks a timeout.

Searching for a goal object is where episodes are most often lost. Follow these rules:
1. Prioritize open, visible receptacles—countertops, dining tables, desks, islands, shelves, dressers, sofa tables, coffee tables—before closed containers. The goal object is usually on an open surface. Do not start a pick-and-place search by opening cabinet 1 or drawer 1 merely because it appears first in the admissible list. Only after several open-surface locations of different kinds have been checked should you begin opening closed containers systematically.
2. Do not tunnel through one container type. If you open two (at most three) cabinets or drawers in a row and none contains the goal object, stop opening the next numbered container of that type. Move to an unvisited location of a different kind (e.g., a counter, table, shelf, or fridge) and search there instead. You need breadth before exhaustive depth.
3. At each location, read the observation once. If it contains the goal object, take it immediately. If not, pick the next still-unvisited location and go there. Treat an empty open surface or opened container as already searched; do not revisit it.

For clean/heat/cool tasks, never wander after acquiring the object. If the item is not at the first location checked, keep searching per the rules above; when you do find it, take it at once, invoke the required state change directly at the appliance while holding it (e.g., clean X with sinkbasin, heat X with microwave 1, cool X with fridge 1), then go straight to the target and place it there. If the target receptacle is closed, open it only at that moment, immediately before placing; do not open it earlier. For pick-two tasks, deliver one copy at a time; when you return to where the remaining required copies are visible, take the next one without hesitation.

When an action is rejected or returns `Nothing happens`, never repeat it and never fall back to `look`. Recover by choosing a different admissible action that moves you toward the current subgoal. If the conversation contains an error or fallback marker, do not continue in that mode; on the next turn output one normal, well-formed think/action pair and proceed from the current location.

After the final required object has been accepted by the target receptacle and your inventory is empty, the episode is over: do not wander off, do not run a chain of confirmation `look`/`examine`/`inventory` actions, and do not take any victory-lap actions. Extra verification tours after goal completion can cost the episode. If required copies still remain, continue only the search-then-deliver sequence until every copy is placed.
<!-- SLOW_UPDATE_END -->

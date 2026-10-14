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

### Pick Two Object Bookkeeping
For `pick_two_obj_and_place`, choose one destination receptacle instance once it is opened/usable, and remember it as the target. Both object instances should be placed into that same remembered receptacle. After placing the first object, do not remove it again; if the second object was already seen, return directly to its remembered location rather than searching randomly. If the two objects are accidentally split across different receptacles, consolidate them into the chosen target receptacle.
| Examine in Light | Examine object X under desklamp | Find X -> take X -> find desklamp -> use desklamp |

| Examine in Light detail | Final interaction | While holding X where a desklamp is visible, use the desklamp; do not try to place X on the lamp first. |
| Clean & Place | Clean object X and put in/on Y | Find X -> take X -> go to sink -> clean X -> go to Y -> put X |
| Heat & Place | Heat object X and put in/on Y | Find X -> take X -> go to microwave -> heat X -> go to Y -> put X |
| Cool & Place | Cool object X and put in/on Y | Find X -> take X -> go to fridge -> cool X -> go to Y -> put X |

---

## General Principles

1. **Decompose the task**: Parse the goal into ordered sub-goals (locate, acquire, transform, deliver). Complete each before moving to the next.
2. **Systematic exploration**: Search each surface and container exactly once before revisiting. Open closed containers (drawers, cabinets, fridge) before judging them empty.

- Prioritize semantically likely locations first, then broaden systematically: food in fridges/on countertops or dining tables; dishes/utensils/cookware on countertops, dining tables, stoveburners, cabinets, or drawers; office/bedroom items on desks, shelves, dressers, sidetables, or in drawers; newspapers on coffeetables, sidetables, sofas, or tvstands; toiletries/cleaning items near sinks, bathroom counters, shelves, carts, or cabinets.

- For portable kitchen targets such as bread, mugs, cups, plates, bowls, and utensils, check broad exposed surfaces early: after one or two empty countertops, try dining tables or other open surfaces before opening many cabinets/drawers. For small office/bedroom targets, alternate drawers with exposed desks, shelves, sidetables, and dressers rather than exhausting drawers first.

- Keep a persistent **searched set** of receptacle instances, e.g. `drawer 1`, `shelf 3`, `countertop 2`. Once an observation shows no needed target object there, mark it searched and do not call it “unexplored” later.
   - If all locations in the current preferred class are searched, **broaden to any unvisited admissible `go to ...` location** instead of restarting the same sequence. Search broadly across surfaces, furniture, containers, and appliances when relevant.
   - If a visible object is itself an openable/container-like object, such as a box, and opening/examining it is admissible, inspect it before leaving the area.
3. **Grab immediately**: When a required object is visible and reachable, take it right away before moving elsewhere.

- Pick up only the exact requested object type. Similar or related objects, such as a cup when the task asks for a mug, a spoon when it asks for a knife, or a pot when it asks for a pan, are distractors; leave them in place and mark that location searched for the target.
4. **Transform before placing**: If the task requires cleaning, heating, or cooling, perform the state change at the appropriate appliance before heading to the final destination.

- Do not repeatedly revisit the sink, microwave, fridge, or final destination before holding the target object. If you find the appliance early, remember its location, then resume searching unvisited object locations until the target object is acquired.

- Use direct admissible appliance/tool commands immediately when available, such as `clean X with sinkbasin`, `heat X with microwave`, `cool X with fridge`, or `use desklamp`. Do not waste steps opening, closing, toggling, or examining the appliance unless the needed action is unavailable or opening is required for searching/placing.
5. **Direct delivery**: Once holding the transformed (or untransformed) goal object, navigate straight to the target receptacle and place it.

- Remember known destination receptacles and return directly to the same instance after pickup/transformation. If the destination is also a semantically likely source location, check/open it early rather than only after exhaustive search: food may already be in the fridge, utensils may be on the diningtable, newspapers may be on/near the sofa, and a target drawer can be opened early for pick-two tasks. If the object starts at the destination but needs cleaning/heating/cooling, take it out, transform it, then return to that same instance and place it back.
6. **Track progress**: Maintain an internal count of how many objects still need to be found and placed. Only stop searching when the count reaches zero.
7. **Avoid loops**: Never repeat the same action more than twice in a row. If stuck, move to a different unexplored location.
8. **Only choose admissible actions**: Always pick an action from the admissible action list. Do not invent actions.

---

## Common Mistakes to Avoid

- **Revisiting searched locations**: Keep track of which surfaces/containers have been checked; do not re-examine them.
- **Ignoring visible objects**: If the target object appears in the observation, pick it up immediately.
- **Skipping state changes**: Do not place an object at the destination without first cleaning/heating/cooling it when required.
- **Premature termination**: Do not stop the episode until all goal conditions are verified as met.
- **Action loops**: Repeatedly toggling or examining the same object wastes steps. Move on to new locations instead.

### Hard Search-Loop Recovery

- **Exact-instance lockout before pickup**: once a receptacle/surface instance has been observed and does not contain the target object, do not go back to that exact instance while still searching for the object. A phase change, such as holding the object or needing final delivery, is the only reason to return.
- **Fast broadening threshold**: after 3-4 misses in the same receptacle class, switch to a different likely class or any unvisited admissible location instead of continuing or restarting that class, unless the target has already been seen there.
- **No search reset by recency**: do not say a location is "unsearched" merely because it was not in the last few observations. The searched set is global for the whole episode.
- **Finite-class exhaustion**: if all visible instances of a small class have been checked once, such as all stoveburners, diningtables, countertops, or shelves, mark that class exhausted for object search and do not start a second pass. Remember a usable destination instance, then search different receptacle classes.
- **Unvisited beats likely-but-searched**: after several misses, prefer any admissible unvisited `go to`, `open`, or `examine` target over revisiting a semantically likely but already-searched location.
- **Destination surfaces before pickup**: if the destination receptacle is also a likely object location, inspect each instance at most once before pickup. If it lacks the object, remember it as the final destination but stop using it as a search target until the object has been transformed and is ready to place.
- **Kitchen item fallback**: for cookware and dishware, after checking obvious burners/tables/counters once, broaden to unsearched cabinets, drawers, shelves, sinkbasins, and other kitchen storage/surfaces rather than cycling among the obvious locations.

### Strict Search Ledger Action Filter

Before every empty-handed search action, apply this hard filter:

1. If a required target object is visible, take it immediately.
2. Otherwise choose an exact receptacle/surface/container instance whose contents have not yet been observed in the current object-search phase.
3. Reject any `go to`, `examine`, or `open` action for an exact instance already observed to lack the target, even if it is semantically likely, nearby, recently mentioned, or the final destination type.
4. If all likely instances are rejected by the ledger, broaden to any unvisited admissible location/class instead of restarting from instance 1 of a searched class.

The searched ledger survives inventory checks, appliance visits, placing the first object in a pick-two task, and putting down an irrelevant inspected object/container. These events are not permission to rescan shelves, drawers, cabinets, tables, counters, or destination receptacles from the beginning.

### Destination-as-Source Lockout

When the final receptacle type is also a plausible source location, inspect each visible destination instance at most once before pickup. After it lacks the target, remember a usable destination instance and lock that exact instance out of object search until you are holding the required object ready for delivery. Do not alternate between destination instances and other searched source instances while still empty-handed.

### Pick-Two Phase Memory

After placing the first object in a pick-two task, do not begin a fresh room/class search. If another required instance was previously seen, return directly to that remembered source location for the second pickup. If no second instance is remembered, continue from the existing unsearched-location ledger rather than revisiting locations already checked before the first placement.

<!-- SLOW_UPDATE_START -->
Preserve the successful pattern: when the exact requested object is visible, take it immediately; perform the required clean/heat/cool/use action as soon as the correct command is admissible; then deliver directly to the remembered destination.

Treat tool locations as tools, not repeated search targets. If a sinkbasin, fridge, microwave, desklamp, or destination receptacle has already been checked and does not contain the target while you are empty-handed, remember it for later but do not revisit it until you are holding the required object or ready to place/use it.

Use a next-unsearched-instance pointer for every numbered class. If you leave cabinets, drawers, shelves, countertops, or stoveburners and later return to that class, resume at the lowest exact instance not yet observed; never restart at instance 1 and never revisit an instance already observed to lack the target.

For pan-to-stoveburner tasks, search in a step-efficient order: make one quick pass over stoveburners only to find a pan or remember an empty destination, then leave stoveburners until delivery. Next check countertops/islands and sinkbasins. Then prioritize cabinets in numeric order, opening each closed cabinet and observing its contents, before low-yield drawers. Do not abandon cabinet search to revisit searched stoveburners, countertops, or drawers.

For kettle/teapot clean-and-place tasks, after checking obvious countertops/islands, check stoveburners and sinkbasins once, then cabinets in numeric order. If several cabinets are empty, continue to the next unsearched cabinet or broaden to unvisited shelves/carts/dining tables; do not return to already searched countertops. Remember one open/empty cabinet as the final destination, but do not keep using searched cabinets as search targets.

For dishsponge clean-and-place tasks, check sinkbasin and nearby countertops once, then search unvisited cabinets, drawers, shelves, carts, and other storage/surfaces. Because the sink is needed for cleaning, remember it after the first visit; do not go back to the sink while empty-handed just because the sponge is likely near it. Because shelf is the destination, remember a usable shelf after inspecting it once; after a shelf lacks the sponge, search only unvisited shelves or other unvisited locations until the sponge is found.

When the step budget is running and you are still empty-handed, prefer any unvisited admissible location over any searched likely location. A location being semantically likely, useful later, or recently mentioned is never a reason to rescan it before acquisition.

Do not let the broadening threshold cause class restarts. Broadening means move to a different unvisited class or continue at the next unsearched instance of a promising storage class; it never means cycling back through exact instances already observed.
<!-- SLOW_UPDATE_END -->

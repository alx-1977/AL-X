# AL/X Schematic Capabilities

*Altium style workflow and development specification*

18 November 2026  |  Version 1.0

Develop AL/X into a dependable schematic engineering assistant by building shared editing, component and verification capabilities. This specification combines the current capability inventory with a prioritized expansion plan. Manual PCB routing remains part of the workflow; schematic automation and safer PCB handover are the focus.

The original inventory below records existing state. Sections 2 to 7 describe proposed functionality; P0 means essential foundation, P1 important, and P2 a later enhancement. These priorities are recommendations and do not change the original statuses.

## 1 Original capability inventory

Original job names, statuses and notes are retained. Blank notes remain blank, and references such as “section 4” remain as written in the original inventory. The source inventory contains 17 rows.

| Job (Altium words) | Status | Notes |
| --- | --- | --- |
| Place a part from the library | Partly | Copies an existing part. Placing by part number or value straight from the FireFli library needs building. |
| Swap a part (change component) | Yes |  |
| Delete parts | Yes |  |
| Wire, net labels, ports, power ports | Yes | In your style: wires for local nets, GND down, power up. |
| No-ERC markers | Yes |  |
| Move or align parts on the sheet | Build | Today she only places new parts. |
| Edit parameters (MPN, LCSC, value) on one or many parts | Build | Needs the select-similar-then-change ability (section 4). |
| Hierarchical sheets and buses | Partly | She converted P0009. She can't yet add a new sheet. |
| Annotate | Build | KiCad does it. Not wired in. |
| Run ERC and read it | Yes |  |
| Update PCB from schematic (ECO) | Yes |  |
| Check a placed circuit against the manufacturer's application diagram | Build | She can fetch and read datasheets, and she checks layouts against reference layouts already. Comparing the schematic part by part and value by value against the app circuit needs building. She shows the datasheet page beside your schematic. |
| Draw a new circuit from the application diagram | Build | Same reading, then she places it with the schematic tools. Values come from the datasheet, and anything unclear goes back to you as a question. |
| SPICE simulation on the schematic | Partly | ngspice runs on a work copy: AC, transient and operating point, plus tuning of R, L and C values. Missing manufacturer models must be supplied. Fetching them and showing plots on screen needs building. |
| Net classes with their rules: impedance, differential pairs, widths, clearances | Build | KiCad has net classes and diff pairs (_P/_N names). Working out the width for an impedance from your stack-up (like the 0.37 mm, about 48 Ω on P0009) is our own calculation, because KiCad's calculator can't be called. The classes then drive routing and DRC. |
| Show wire crossings as a half-circle hop | Build (KiCad setting) | KiCad 10 has it: "Hop-over size" in Schematic Setup. P0009 has it off. In a test, the PDF output didn't draw the hop at any size, so it may show only in the editor. To be checked on screen. |
| Tidy up the schematic: no overlapping text, nothing in the title block | Partly | Text tidy for parts she touches, and new wiring keeps off the title block. A whole-sheet tidy that moves parts and checks the title block area needs building. |

## 2 Editing and manipulation

PROPOSED FUNCTIONALITY  |  P0 essential  ·  P1 important  ·  P2 later

Provide reliable single-object and bulk operations, with explicit control over whether an edit changes geometry, electrical connectivity, or both.

| Capability | Required behavior | Priority |
| --- | --- | --- |
| Move vs Drag | Move a component with or without maintaining its wires, exactly as intended. | P0 |
| Rotate / Mirror / Flip | Rotate or mirror one or multiple symbols, keeping pin connections correct. | P0 |
| Copy / Paste / Duplicate | Duplicate circuits, preserve their topology and assign new designators. | P0 |
| Select Similar Objects | Select components based on value, MPN, footprint, type, sheet, etc. | P0 |
| Find and Replace | Search and replace properties, net names, values and designators across all sheets. | P0 |
| Smart wire editing | Extend, shorten, split, join, reroute and remove wires without leaving accidental disconnections. | P0 |
| Insert component into a wire | Insert a resistor, ferrite bead or other series component and automatically reconnect the two sides. | P0 |
| Pin and gate swapping | Swap equivalent op-amp units, logic gates or MCU pins with electrical validity checks. | P1 |
| Group / Ungroup | Group related circuit objects, then move, duplicate or edit them together. | P1 |
| Align / Distribute | Align by position or pin, distribute evenly and organize component spacing. | P1 |
| Undo / Redo | Reverse or reapply AL/X operations, including multi-step edits. | P0 |
| Visual formatting | Edit text sizes, visibility, rotations, colors, line styles and annotations. | P1 |

**Connection intent.** Move and drag must be distinct operations. A moved IC or circuit group must retain exactly the connections intended by the selected operation.

**Shared editing behavior.** Selection, property changes and wire operations should use the same editing engine. Investigate the installed KiCad version’s native properties, grouping, alignment and swapping functions before implementing equivalent behavior.

## 3 Library and component management

PROPOSED FUNCTIONALITY  |  P0 essential  ·  P1 important  ·  P2 later

Use the FireFli library as the source of truth, with a consistent component schema and controlled links between symbols, footprints, manufacturer parts and supplier records.

| Capability | Required behavior | Priority |
| --- | --- | --- |
| Create new library parts | Generate a new symbol from a datasheet, with all pins, electrical types and component fields. | P0 |
| Edit library symbols | Modify pin names, numbers, graphical symbols and electrical types. | P0 |
| Multi-unit components | Create and manage parts with multiple units, such as U1A/U1B/U1C. | P1 |
| Footprint assignment | Associate the correct footprint from the FireFli library. | P0 |
| Pin-to-pad verification | Compare schematic pin numbers against the footprint and manufacturer's package specification. | P0 |
| Update from library | Detect changes in a library component and safely propagate approved changes to placed instances. | P0 |
| Component validation | Verify required fields, duplicate pins, missing footprint, missing MPN and invalid mappings. | P0 |
| Component alternatives | Identify possible substitutes, checking pin compatibility, parameters and footprint compatibility. | P1 |
| Component lifecycle | Flag obsolete, NRND or unavailable components and distinguish verified from unverified data. | P1 |
| Footprint / 3D verification | Confirm package dimensions, pad geometry, orientation and 3D model assignment. | P1 |
| Library version tracking | Record exactly which approved component revision was used in a project. | P1 |

**Component integrity.** Changing a 10 kΩ resistor to 4.7 kΩ must not silently introduce an incompatible footprint or supplier identity.

**Datasheet review.** For new ICs, verify package-specific pin numbering and electrical types. Ambiguous source information must be presented for review rather than accepted as fact.

## 4 Hierarchical design and reusable circuits

PROPOSED FUNCTIONALITY  |  P0 essential  ·  P1 important  ·  P2 later

Extend hierarchy beyond conversion of an existing design. AL/X should manage sheet structure, signal scope and reusable circuitry as complete engineering objects.

| Capability | Required behavior | Priority |
| --- | --- | --- |
| Create / Delete / Rename sheets | Manage complete schematic structures and page organization. | P0 |
| Hierarchical port synchronization | Automatically create, rename and update corresponding sheet pins and labels. | P0 |
| Net scope management | Correctly distinguish local nets, global nets, power nets and hierarchical connections. | P0 |
| Reusable circuit blocks | Save a known-good design and instantiate it in another project. | P1 |
| Parameterized circuit blocks | Reuse a circuit while overriding parameters such as output voltage or resistance values. | P1 |
| Repeated channels | Instantiate identical sensor, analog or driver circuits with unique channel connections. | P1 |
| Cross-sheet references | Display and navigate page references for signals spanning multiple sheets. | P1 |
| Signal harnesses | Provide Altium-like named signal bundles where KiCad buses are insufficient. | P2 |

**Reuse opportunity.** Evaluate native KiCad design blocks for reusable schematic fragments and associated PCB layouts. A tested, manually routed circuit could then be reused where its constraints remain valid.

**Net identity.** Repeated channels and copied blocks must receive distinct identities and connections while retaining the intended internal topology.

## 5 Engineering verification

PROPOSED FUNCTIONALITY  |  P0 essential  ·  P1 important  ·  P2 later

Treat application-circuit comparison as part of a broader verification system. Use manufacturer data, explicit requirements and calculations to assess whether the circuit is suitable for its intended use.

| Capability | Required behavior | Priority |
| --- | --- | --- |
| Net connectivity verification | Trace every net and confirm actual pin-to-pin connectivity, not just visually touching wires. | P0 |
| Intelligent ERC resolution | Explain errors, propose corrections and apply approved fixes. | P0 |
| Power tree analysis | Calculate voltage rails, estimated current requirements and regulator power dissipation. | P1 |
| Component ratings | Check voltage, current, power, tolerance and temperature derating. | P1 |
| Pull-up / Pull-down validation | Verify values and logic levels, including I²C pull-up strength and rise-time estimates. | P1 |
| MCU pin validation | Check peripheral functions, alternate pin assignments, boot states and conflicting uses. | P1 |
| Reference design compliance | Compare an implemented circuit with datasheet recommendations and identify discrepancies. | P0 |
| Circuit calculations | Calculate resistor dividers, RC filters, regulator feedback, LED resistors, oscillator networks, etc. | P1 |
| Protection circuit checks | Identify missing or questionable ESD, reverse-polarity, input protection and decoupling provisions. | P1 |
| Simulation improvements | Run parameter sweeps, compare design cases and evaluate tolerances where valid models exist. | P2 |
| Design intent checks | Store requirements such as a rail's intended voltage or a signal's required pull-up and verify them after edits. | P1 |

**Beyond ERC.** A schematic can pass ERC while using an unsuitable pull-up resistor, overloading an LDO, or assigning an MCU pin to an incompatible peripheral.

**Evidence and uncertainty.** Distinguish verified data, calculated results and unresolved assumptions. Do not present a plausible calculation as a verified engineering fact.

## 6 Schematic and PCB integration

PROPOSED FUNCTIONALITY  |  P0 essential  ·  P1 important  ·  P2 later

Reduce the manual layout workload through accurate cross-probing, controlled synchronization and complete constraints, while preserving existing PCB work.

| Capability | Required behavior | Priority |
| --- | --- | --- |
| Cross-probing | Select a component or net in the schematic and identify its exact counterpart on the PCB, and vice versa. | P0 |
| ECO preview / comparison | Show exactly what will change before applying schematic updates to the PCB. | P0 |
| Preserve PCB work | Avoid unexpectedly moving footprints, changing identities or losing existing routing during updates. | P0 |
| Back annotation | Bring compatible PCB-side changes, including approved pin swaps, back into the schematic. | P1 |
| Component classes | Group components by circuit block or functional purpose to help with PCB organization. | P1 |
| Net ties / jumpers | Model intentional electrical connections that require special PCB treatment. | P1 |
| Test-point management | Identify critical nets and assign or verify test points. | P1 |
| Layout directives | Pass requirements for sensitive, high-current, differential or impedance-controlled nets into PCB constraints. | P1 |
| PCB readiness audit | Verify footprints, pin mapping, net classes, missing models and required constraints before layout starts. | P0 |

**Safe swaps.** Pin and gate swaps require an electrical equivalence and permission check. Availability of a native swap command is not proof that two pins are interchangeable.

**Preserving layout.** Preview the effect of each ECO on component identity, footprints and nets before applying it. Existing placement and routing must remain intact unless the approved change requires otherwise.

## 7 BOM and outputs

PROPOSED FUNCTIONALITY  |  P0 essential  ·  P1 important  ·  P2 later

Keep the manufacturing and documentation workflow consistent with approved component data, assembly choices and design revisions.

| Capability | Required behavior | Priority |
| --- | --- | --- |
| BOM generation | Generate a structured BOM using the FireFli component library and approved supplier records. | P0 |
| BOM validation | Check missing MPNs, supplier references, duplicate entries and DNP rules. | P0 |
| Design variants | Manage assembly versions with alternate values, footprints and fitted/not-fitted parts. | P1 |
| Project templates | Create projects with predefined sheet sizes, title blocks, grids, fields and rules. | P1 |
| Drawing documentation | Add engineering notes, revision data, sheet descriptions and linked datasheets. | P1 |
| Release package | Generate schematic PDFs, BOMs, netlists, ERC reports and other required outputs. | P1 |
| Design revision comparison | Show what changed between two schematic revisions, including electrical connections. | P1 |
| Design approval | Mark circuits or complete sheets as reviewed and approved, protecting them from unintended changes. | P1 |

**Variants.** Evaluate native KiCad variant support and its fitted status and field overrides before choosing an implementation. Validate whether alternate footprints require additional handling.

**Manufacturing continuity.** Consistent component and footprint metadata also supports eventual NeoDen YY1 placement preparation.

## 8 Five foundational architectural capabilities

Every schematic operation should use these shared capabilities. Establishing this foundation and its acceptance tests is the central recommendation for reducing repeated patches and inconsistent behavior.

### 1 Schematic object and connectivity model

Represent symbols, pins, nets, wires, labels, sheets, component fields and their relationships. Distinguish drawing geometry from electrical connectivity so that a visual contact is never treated as sufficient proof of a connection.

### 2 Universal selection and editing

Provide a reusable selection and filtering engine and a common operation layer for create, read, update, move, connect, disconnect and delete. Bulk commands should compose these operations rather than introduce separate implementations.

### 3 Transactional edits with verification

Take a snapshot, calculate the intended changes, verify the resulting netlist and rules, and then commit or roll back. Preserve stable object identities so that later PCB synchronization remains safe.

### 4 Engineering rules and design intent

Centralize schematic style, electrical rules, accepted component data and circuit requirements. Preserve local wiring conventions, GND down and power up, without embedding those decisions independently in each tool.

### 5 Native KiCad adapter

Separate AL/X engineering logic from the mechanism that edits KiCad. Use supported interfaces where available, with carefully tested file-level operations where needed. The adapter should absorb version-specific changes without forcing a rewrite of the engineering layer.

### Integration planning requirement

Check the installed KiCad version and supported schematic automation interfaces before selecting the integration path. The conversation identified live schematic API coverage and open-editor synchronization as constraints requiring explicit investigation. Any file-level change must handle editor state, saving and reloading safely and be tested against the actual supported version.

## 9 Acceptance testing criteria

A successful one-off command is insufficient evidence of a dependable capability. Each operation needs repeatable acceptance tests covering its intended result, preservation of unaffected design data, and recovery from failure.

### Move component completion criteria

The conversation explicitly defines completion as all of the following: move with wires preserved; move without wires; move a multiple-component selection; maintain grid alignment; preserve unaffected net connectivity; undo the change; and confirm that the resulting schematic opens correctly.

### Proposed acceptance checklist for all capabilities

The criteria below extend that example into a reusable test standard for the feature set.

| Test area | Acceptance evidence |
| --- | --- |
| Intended result | The requested objects, fields, nets or sheets change as specified. Selection filters match the intended scope and exclude unrelated objects. |
| Connectivity and identity | Compare the before and after connectivity graph or netlist. Only intended connections change; unaffected object identities and pin mappings remain stable. |
| Valid schematic and presentation | Save, reopen and inspect the result. Symbols, labels and wires remain on the intended grid, readable and clear of the title block. |
| Undo and recovery | Undo and redo restore the correct state. A failed multi-step operation rolls back without leaving a partly edited project. |
| Component and library integrity | Verify required fields, package-specific pin-to-pad mapping, footprint compatibility and library revision. Ambiguous inputs trigger review. |
| Hierarchy and reuse | Check sheet pins, labels and net scope. Duplicated channels have unique identities without unintended cross-channel connections. |
| Rules and engineering checks | Run ERC and applicable design-intent checks. Explain new findings and record justified exceptions; an ERC pass alone does not establish electrical suitability. |
| PCB and output consistency | Match ECO preview to the applied change and preserve unaffected routing and placement. Verify BOM quantities, DNP handling and outputs against the selected design state. |
| Repeatability and recorded evidence | Record the test case, relevant software version, before and after state, checks performed, outcome and unresolved limitations. Repeat representative regression cases after changes. |

## 10 Recommended implementation sequence

Use the following foundation sequence from the conversation to organize delivery. Prioritize reusable capabilities before adding every proposed command; retain the P0, P1 and P2 classification on individual features.

### 1 Reliable schematic object and connectivity model

Establish the shared representation of objects, nets and identity that all edits and verification depend on.

### 2 Universal selection properties and bulk editing

Expose consistent filters and property operations so that single-object and multi-object commands follow one path.

### 3 Complete component creation and library synchronization

Build controlled part creation, footprint assignment, pin verification and propagation from the FireFli library.

### 4 Smart wire editing move and drag

Implement connection-preserving manipulation and deliberate disconnection, with predictable wire behavior.

### 5 Hierarchical sheet creation and net management

Add sheet management, hierarchical port synchronization and explicit net-scope handling.

### 6 Transactional changes regression tests and ERC verification

Integrate snapshots, commit and rollback behavior, repeatable regression coverage and rule verification across the workflow.

### 7 Engineering checks and datasheet based circuit generation

Extend verified editing into application-circuit comparison, engineering calculations and circuit generation with review of ambiguous data.

### Delivery interpretation

Treat the sequence as an integration roadmap. Define transaction boundaries and acceptance tests while designing the foundation, then consolidate their end-to-end coverage in stage 6. P1 and P2 features should build on the same architecture as their prerequisites become available.

A command such as “select all 100 nF capacitors and change their footprint” should become a composition of established selection, library validation and transactional editing capabilities.

Specification source: Expand Schematic Capabilities conversation.

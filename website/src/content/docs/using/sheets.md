---
title: Sheets and layouts
description: Print onto label sheets with a page printer, keep track of used cells and lay out tape labels.
---

A page printer prints onto label sheets: A4 or letter pages with a grid of labels. Labelyard keeps track of every physical sheet, so a sheet you only half used can go back into the tray and be filled later.

## Sheet types

A **sheet type** describes a product: page size, the number of rows and columns and the measurements of a cell. Open **Sheets** to create one or to import one. Sheet types are versioned: when you correct a measurement, you add a version, and sheets that were already in use keep the version they were printed with.

Sheet types marked **verified** were measured on real sheets. Labelyard 1.7.0 adds Avery Zweckform 6121, 3490 and L4737REV (3490 has its sheet code printed on the back of the liner, see [Where the sheet code goes](#where-the-sheet-code-goes)) and corrects L4731REV (sheets you already have keep the old measurement). A type you created or imported under the same key is taken over with your prefix and its older versions kept, and the bell tells you once. A taken-over type counts as shipped from then on.

You can export a sheet type to a file and import it in this or another hub, so a product only has to be measured once.

### The sheet archive: installed types and the catalog

The **Sheet archive** has two tabs.

**Catalog** lists the sheet types Labelyard ships. Search by vendor, product code or label size (type `45.7 x 21.2` for a size), pick a page size or a vendor, and tick **Verified only** to hide entries nobody has checked on a real sheet. Each entry says whether it is **Verified** (with the day) or **Not yet validated**, and **Show details** opens its grid with the measurements and where the values come from. **Add** installs an entry with one click. An installed entry shows **Installed**; when the catalog has a newer version of a type you did not change, it shows **Update**, which opens the type with the change. If another type already uses the entry's code prefix, the entry offers a free prefix to add it with instead.

A fresh installation starts with no sheet types: the **Installed** tab then says so and opens the catalog. An installation upgraded from an earlier version keeps every type it has.

**Installed** lists your types with their usage: how many sheets they have, how many of those have free cells, how many free labels are left, when a sheet of the type was last used, and a state: **In use**, **Used before** or **Unused**. Filter by state or by **Not used since** a day, and sort by key, usage or last use.

**Updates from the catalog.** A type you did not change shows **Update available** with the measurements the new version changes, old and new side by side, and **Update to catalog version N**. Sheets you already have keep the version they were made with. When the update also changes the backing design that another type shares, that type is updated with it and named after **Also updated**. A type you changed yourself only says that the catalog has a newer version; export it to compare, or delete it and add it again.

**Deleting.** Any type without sheets can be deleted, also a type from the catalog. While a type has sheets, **Delete type** waits and the archive offers **Archive** instead. **Remove unused types** lists exactly the types without any sheet, with none ticked; tick the ones to remove. Sheet codes are never given out twice: a type you add back continues its numbering.

**Versions.** The **Versions** tab of a type lists its versions, newest first, each with the measurements it changed against the version before (old → new), who added it and when, and its change note. An admin can take an older version's measurements back with **Restore as new version**: it adds a new version with the note "Restored from version N", and sheets keep the version they were made with. While a sheet uses a newer version, the older one is only shown.

**From a type to its sheets and back.** The details of a type list its sheets, those in use first, then the used-up ones; discarded sheets appear with **Show discarded**. Each code opens its sheet, and **Show in the sheets list** opens **Sheets** filtered by the type. The other way round, every sheet names its type with the version it was made with, in the list and on its page, as a link to the type in the archive.

Every sheet type has its own switch **QR codes on labels**, in the details of the type in the **Sheet archive**. A new type starts with it on when the shorter side of a label is at least 10 mm: on 25.4 × 10 mm labels a QR code with a short number beside it prints and scans. With the switch off, the labels on that type are printed without their QR code, the sheet ID label included, and the print preview marks those cells with "Code only on this sheet type". Types that already exist keep the setting they have.

## Sheets and their ID label

Each physical sheet gets its own code. When you add a sheet, you can print an ID label for it right away, so you can scan it later. A sheet knows which of its cells are free and which are used.

- **Free cells** take the next labels.
- **Unconfirmed cells** are cells of a job whose outcome is not known, for example when the printer failed mid-page. Check the sheet and confirm which of them were printed, one by one. When there are two or more, **Resolve all as printed** and **Resolve all as not printed** settle them in one go; the hub asks first and names how many cells it is about. If those cells changed in the meantime (another one became unconfirmed or one was settled elsewhere, even when the number stayed the same), nothing is written and you are asked to check the list again.
- **Discarding** a sheet takes it out of the picker. Its code is never given out again; labels already printed stay printed.

## Where the sheet code goes

The sheet code identifies a sheet when you scan it, for example `A3490-0001`. Each sheet type says where the hub prints it; change it in the **Sheet archive**, tab **Sheet code**. The chosen place is shown as one card marked **Chosen** (**Chosen automatically** while the type keeps the automatic choice); the line **Other choices** below it switches to another one:

- **In the margin**: the code goes in the free band around the labels, with the sheet's first print. No extra pass.
- **Back of the liner**: the code goes on the back of the liner, in a pass of its own.
- **A label cell**: one cell of the sheet holds the code and takes no label. You choose the cell; it is left out of planning, and when it is already used the code takes the first free cell.

A new sheet type chooses automatically: the margin when a band of 4 mm fits on the printer, otherwise the back side. The card marks that place as **Default for this sheet**. Avery Zweckform 3490 has no usable margin, so its code goes on the back.

**Activating a sheet with its code on the back.** When you add such a sheet, the hub guides you in three steps: turn the sheet over along the long edge and load it top edge first, print the sheet code, then check it. The code counts as printed only after you answer **Yes, it is printed**: a finished job says nothing about smeared toner. If it smeared, **Print again** prints on the same sheet. If you close the dialog before you confirm, the sheet page offers **Print back side** later. Until then the sheet list marks the sheet **Back side missing**, and the filter of the same name shows only those sheets. The first time, the hub asks how the tray takes paper (see [How a tray takes paper](/using/printers/#how-a-tray-takes-paper)).

**Backing designs.** Many liners carry the vendor's print on the back. A backing design names its free strips, and the code goes into the first strip that takes it. Labelyard ships the design of Avery Zweckform ultragrip and links it to 3490 and 6121. With the back of the liner chosen, pick a design, or a plain liner, in the same tab; **Automatic** in the other choices returns a type to the automatic choice; adding or changing a design is done through the API (`/api/v1/backing-designs`).

**The sheet code in the margin.** With the margin chosen, the tab shows **Position in the margin**: the **Band** (**Automatic** takes the largest free band; a band you choose is a rule, and a printer with no room there prints no code in the margin), **Start from** (**Left or top**, **Right or bottom**) and **Offset from that end**, the distance from the end of the label grid to where the code starts, in steps of 0.5 mm. The field takes a decimal comma or point. **Save position** saves these three alone. Below, **On your printers** lists every enabled page printer with the tray its sheets use; **Check on tray** on a row checks that printer on another of its trays, and **Chosen place** follows it: **Fits** with the place and what is printed (QR + code + type text, QR + code, or the code as text), or **Does not fit** with the reason in words and its code (`too_small`, `overlaps_preprint`, `outside_printable`, `no_margin_band`). **Chosen place** draws the sheet front with the code where the chosen printer puts it.

**Preprinted areas.** Many sheets carry the vendor's print on the front too, a logo or a product line in the margin. **Edit details** of a type holds them under **Preprinted areas**: **Add area** asks for x, y, width and height in mm from the sheet's top-left corner and an optional note; a type has at most 16 areas. A **Vendor preset** adds its areas above your own, marked **Preset**; **Unlink preset and keep a copy** turns them into your own areas so you can change them. The hub keeps the sheet code, its QR code and the feed mark out of every area. An area may not cover a label: the dialog warns while you type, and **Save details** refuses it, names the area and marks it in the list (`preprint_zone_overlaps_cell`). The diagram beside the form draws the areas hatched and the planned sheet code outlined, and the print preview shows the areas hatched over the page. **Sheet code position** in the same form holds the band, start and offset as well.

A sheet that you start from the print dialog, or that a Grocy label opens, follows its type in the same way. A margin sheet whose code was never printed, such as a 3490 sheet from before 1.7.0, can switch to the back side when its type puts the code there: open it and choose **Print back side**. A sheet whose code you added by hand keeps it and gets the code on the back as well (see below).

**The Sheet code panel.** The page of every sheet has a panel **Sheet code** next to the grid. It names where the code is (in a cell, in the margin, added by hand, on the back of the liner) and whether it is printed. When a sheet has a code on the back, or one is asked for, the panel adds **Back side: missing** until you confirm it, then **Back side: confirmed**. A sheet that never asked for a code on the back has no such line and is not marked **Back side missing**. A key that may print sees the buttons:

- **Reprint margin code** prints the code into the margin of a margin sheet again, for example when the first one smeared or was cut off. Put the sheet back the way it went in the first time and choose the printer and tray. Only the code is printed: no cell is used and no label is printed. The hub picks the margin band again for that printer and tray, so on another printer the code can land in another corner than the first one. While a margin code is still on its way to the printer, a second one is refused.
- **Print back side** also works on a sheet whose code is already printed in a cell or in the margin, and on every sheet whose code you added by hand. The sheet keeps its code where it is and gets the same code on the back as well. Until you confirm it with **Yes, the back side is printed**, the list marks the sheet **Back side missing**.

## Printing onto a sheet

Choose **Print onto a sheet**, pick the sheet (or scan its code) and the cells. The preview shows where every label goes. When not everything fits, the rest stays in the [cart](/using/cart/) for the next sheet, or the preview plans it on the next one.

If another device used cells of the same sheet while you were looking, the hub tells you and asks you to check the new plan before it prints.

**The job of a cell.** In the API a cell names the job that reserved or printed it (`job_id`). That is a pointer, not a grant: `GET /api/v1/jobs/{id}` answers `404` for a key that may not use the job's printer (`allowed_printer_ids`), and for a job another plain key submitted, exactly as for an unknown job. A key that may read the sheet but not the printer sees the number of the job and nothing of its content.

## Layouts

A layout decides what a label shows: a QR code, a code, a title and a detail line, in combinations from "code only" to "QR code, code, title and detail". Layouts that need a field the item does not have are refused before printing, with the missing field named. On a sheet type with QR codes switched off, a QR layout prints its text only (see [Sheet types](#sheet-types)).

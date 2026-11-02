"""Form Accessibility Analyzer: labels, instructions, error announcements, and ARIA mappings."""
from __future__ import annotations

from typing import Any
from playwright.async_api import Page


async def inspect_form_accessibility(page: Page) -> list[dict[str, Any]]:
    """Analyzes all form controls on the page for labels, instructions, and error bindings."""
    return await page.evaluate(
        """() => {
          const controls = document.querySelectorAll('input:not([type="hidden"]), select, textarea');
          const results = [];

          controls.forEach((el, idx) => {
            const id = el.id;
            const name = el.name || '';
            const type = el.type || el.tagName.toLowerCase();
            const hasAriaLabel = !!el.getAttribute('aria-label');
            const hasAriaLabelledBy = !!el.getAttribute('aria-labelledby');
            const ariaDescribedBy = el.getAttribute('aria-describedby');
            const ariaInvalid = el.getAttribute('aria-invalid');
            const ariaErrorMessage = el.getAttribute('aria-errormessage');
            const placeholder = el.getAttribute('placeholder');

            // Find associated <label for="...">
            let associatedLabel = null;
            if (id) {
              const lbl = document.querySelector(`label[for="${CSS.escape(id)}"]`);
              if (lbl) associatedLabel = lbl.innerText.trim();
            }
            if (!associatedLabel && el.closest('label')) {
              associatedLabel = el.closest('label').innerText.trim();
            }

            const accessibleName = hasAriaLabel
              ? el.getAttribute('aria-label')
              : (associatedLabel || (hasAriaLabelledBy ? 'linked_by_id' : null));

            const hasName = !!accessibleName;
            const onlyPlaceholder = !hasName && !!placeholder;

            results.push({
              index: idx,
              id: id || null,
              name: name || null,
              type: type,
              has_accessible_name: hasName,
              accessible_name: accessibleName,
              associated_label: associatedLabel,
              has_aria_describedby: !!ariaDescribedBy,
              described_by_id: ariaDescribedBy,
              is_invalid: ariaInvalid === 'true',
              has_error_message: !!ariaErrorMessage,
              only_placeholder_as_label: onlyPlaceholder,
              required: el.required || el.getAttribute('aria-required') === 'true',
              selector: id ? `#${id}` : (name ? `[name="${name}"]` : `${el.tagName.toLowerCase()}:nth-of-type(${idx+1})`),
            });
          });

          return results;
        }"""
    )


def summarize_form_defects(form_data: list[dict[str, Any]]) -> list[dict[str, Any]]:
    defects = []
    for item in form_data:
        if item.get("only_placeholder_as_label"):
            defects.append({
                "type": "placeholder_only_label",
                "severity": "medium",
                "selector": item["selector"],
                "message": "Form control relies solely on placeholder text as its label.",
                "suggested_fix": "Provide a persistent visible <label> alongside the placeholder.",
            })
        elif not item.get("has_accessible_name"):
            defects.append({
                "type": "missing_form_label",
                "severity": "high",
                "selector": item["selector"],
                "message": f"Form control <{item['type']}> '{item['id'] or item['name']}' has no associated <label> or aria-label.",
                "suggested_fix": f"Add <label for=\"{item['id'] or 'field'}\">Label text</label> or aria-label attribute.",
            })
    return defects

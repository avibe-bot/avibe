import { clsx, type ClassValue } from "clsx"
import { extendTailwindMerge } from "tailwind-merge"

// The elevation shadows (index.css `--shadow-elevation-*`) are box-shadow sizes,
// so a caller's `shadow-none` or `shadow-elevation-3` replaces a primitive's
// default instead of both landing in the class list.
const twMerge = extendTailwindMerge({
  extend: { theme: { shadow: ["elevation-0", "elevation-1", "elevation-2", "elevation-3", "elevation-3-up"] } },
})

export function cn(...inputs: ClassValue[]) {
  return twMerge(clsx(inputs))
}

export async function copyTextToClipboard(text: string): Promise<boolean> {
  if (typeof navigator !== 'undefined' && navigator.clipboard?.writeText) {
    try {
      await navigator.clipboard.writeText(text)
      return true
    } catch {
      // Fall through to the legacy copy path.
    }
  }

  if (typeof document === 'undefined' || !document.body) {
    return false
  }

  const textarea = document.createElement('textarea')
  textarea.value = text
  textarea.setAttribute('readonly', '')
  textarea.style.position = 'fixed'
  textarea.style.top = '0'
  textarea.style.left = '0'
  textarea.style.opacity = '0'
  textarea.style.pointerEvents = 'none'

  const activeElement = document.activeElement instanceof HTMLElement ? document.activeElement : null
  const selection = document.getSelection()
  const originalRange = selection && selection.rangeCount > 0 ? selection.getRangeAt(0) : null

  document.body.appendChild(textarea)
  textarea.focus()
  textarea.select()
  textarea.setSelectionRange(0, textarea.value.length)

  try {
    return document.execCommand('copy')
  } catch {
    return false
  } finally {
    document.body.removeChild(textarea)

    if (selection) {
      selection.removeAllRanges()
      if (originalRange) {
        selection.addRange(originalRange)
      }
    }

    activeElement?.focus()
  }
}

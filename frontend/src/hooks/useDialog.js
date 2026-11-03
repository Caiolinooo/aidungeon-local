import { useEffect, useRef } from 'react'

const FOCUSABLE_SELECTOR = [
  'a[href]',
  'button:not([disabled])',
  'textarea:not([disabled])',
  'input:not([disabled]):not([type="hidden"])',
  'select:not([disabled])',
  '[tabindex]:not([tabindex="-1"])',
].join(',')

function focusableElements(root) {
  return Array.from(root.querySelectorAll(FOCUSABLE_SELECTOR)).filter((el) => {
    if (el.tabIndex < 0) return false
    if (el.closest('[inert], [hidden]')) return false
    return el.getClientRects().length > 0
  })
}

// Enquanto `open`: Escape chama onClose, foco entra no container, Tab fica preso
// e o foco anterior volta ao fechar. O caller define role="dialog" e aria-modal.
export function useDialog(open, onClose) {
  const ref = useRef(null)
  const onCloseRef = useRef(onClose)
  onCloseRef.current = onClose

  useEffect(() => {
    if (!open) return undefined
    const node = ref.current
    if (!node) return undefined

    const previouslyFocused = document.activeElement

    const focusContainer = () => {
      if (!node.hasAttribute('tabindex')) node.setAttribute('tabindex', '-1')
      node.focus()
    }

    const items = focusableElements(node)
    if (items.length > 0) items[0].focus()
    else focusContainer()

    const onKeyDown = (event) => {
      if (event.key === 'Escape') {
        event.preventDefault()
        event.stopPropagation()
        onCloseRef.current?.()
        return
      }
      if (event.key !== 'Tab') return

      const current = focusableElements(node)
      if (current.length === 0) {
        event.preventDefault()
        focusContainer()
        return
      }
      const first = current[0]
      const last = current[current.length - 1]
      const active = document.activeElement
      const outside = !node.contains(active)
      if (event.shiftKey) {
        if (active === first || outside) {
          event.preventDefault()
          last.focus()
        }
      } else if (active === last || outside) {
        event.preventDefault()
        first.focus()
      }
    }

    document.addEventListener('keydown', onKeyDown, true)
    return () => {
      document.removeEventListener('keydown', onKeyDown, true)
      if (
        previouslyFocused instanceof HTMLElement &&
        previouslyFocused.isConnected &&
        previouslyFocused !== node &&
        !node.contains(previouslyFocused)
      ) {
        previouslyFocused.focus()
      }
    }
  }, [open])

  return ref
}

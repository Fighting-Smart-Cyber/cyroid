// frontend/src/utils/sanitizeHtml.ts
import DOMPurify from 'dompurify'

/**
 * Sanitize stored content HTML before it reaches the DOM.
 *
 * `Content.body_html` is a cached render of author-written markdown, so it is
 * user-authored even though it arrives from our own API — which makes it a
 * stored-XSS vector, not trusted input.
 *
 * The allow-list is the set of tags and attributes TipTap emits, so sanitizing
 * is lossless for legitimate content while dropping scripts, event handlers and
 * anything else outside it.
 *
 * Extracted from ContentEditor, where it lived as a module-local function while
 * ContentLibrary's PDF path assigned raw `innerHTML` with an inert
 * `eslint-disable` comment for a plugin that was never installed.
 */
export function sanitizeHtml(html: string): string {
  return DOMPurify.sanitize(html, {
    ALLOWED_TAGS: [
      'h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'p', 'br', 'hr',
      'strong', 'b', 'em', 'i', 'u', 's', 'strike', 'code', 'pre',
      'ul', 'ol', 'li', 'blockquote',
      'a', 'img',
      'table', 'thead', 'tbody', 'tr', 'th', 'td',
      'div', 'span',
    ],
    ALLOWED_ATTR: ['href', 'src', 'alt', 'title', 'class', 'target', 'rel', 'colspan', 'rowspan'],
  })
}

// frontend/src/pages/ContentEditor.tsx
import { useState, useEffect, useCallback } from 'react'
import { isAxiosError } from 'axios'
import { useParams, useNavigate } from 'react-router-dom'
import { useEditor, EditorContent } from '@tiptap/react'
import StarterKit from '@tiptap/starter-kit'
import Placeholder from '@tiptap/extension-placeholder'
import Image from '@tiptap/extension-image'
import Link from '@tiptap/extension-link'
import { Table } from '@tiptap/extension-table'
import { TableRow } from '@tiptap/extension-table-row'
import { TableCell } from '@tiptap/extension-table-cell'
import { TableHeader } from '@tiptap/extension-table-header'
import CodeBlockLowlight from '@tiptap/extension-code-block-lowlight'
import { common, createLowlight } from 'lowlight'
import { sanitizeHtml } from '../utils/sanitizeHtml'
import {
  Save,
  ArrowLeft,
  Eye,
  EyeOff,
  Bold,
  Italic,
  Strikethrough,
  Code,
  List,
  ListOrdered,
  Quote,
  Minus,
  Heading1,
  Heading2,
  Heading3,
  Link as LinkIcon,
  Image as ImageIcon,
  Table as TableIcon,
  Undo,
  Redo,
  Upload,
  X,
  Copy,
  Tag,
  AlertTriangle,
} from 'lucide-react'
import { contentApi, Content, ContentCreate, ContentUpdate, ContentType, ContentAsset } from '../services/api'
import { WalkthroughEditor } from '../components/content/WalkthroughEditor'
import type { Walkthrough } from '../types'

const lowlight = createLowlight(common)

const CONTENT_TYPES: { value: ContentType; label: string }[] = [
  { value: 'student_guide', label: 'Student Guide' },
  { value: 'msel', label: 'MSEL' },
  { value: 'curriculum', label: 'Curriculum' },
  { value: 'instructor_notes', label: 'Instructor Notes' },
  { value: 'reference_material', label: 'Reference Material' },
  { value: 'custom', label: 'Custom' },
]

// sanitizeHtml moved to src/utils/sanitizeHtml.ts so ContentLibrary's PDF path
// can use the same allow-list instead of assigning raw innerHTML.

// ---------------------------------------------------------------------------
// TipTap HTML -> Markdown
//
// `body_markdown` is the stored form. The API re-renders `body_html` from it on
// every save and the editor reloads that render, so whatever this serialiser
// cannot express is destroyed the moment the author presses Save -- and the
// destroyed version is what they are handed back. What this replaced was a
// chain of regexes over the raw HTML: the `<code>` pattern had no `s` flag, so
// a multi-line code block never matched and fell through to the catch-all tag
// strip; every `<li>` became a `-` bullet whatever its parent; and `<table>`,
// `<pre>` and `<s>` had no rule at all. Four of the toolbar's own buttons --
// Table, Code Block, Ordered List and Strikethrough -- produced content that
// looked right while editing and was flattened on save.
//
// The input is never arbitrary HTML. It is ProseMirror's own serialisation,
// which closes every tag and quotes every attribute, so a small parser over the
// string is sound. Keeping it string-only also keeps it testable: the unit
// tests run in vitest's node environment, which has no DOM.
//
// The target dialect is what the API renders with: Python-Markdown plus its
// `tables` and `fenced_code` extensions. That is not GFM -- it has no `~~`
// strikethrough -- so strikethrough goes out as an inline `<s>` element, which
// Python-Markdown passes through and TipTap's Strike mark parses back.
// ---------------------------------------------------------------------------

interface HtmlElement {
  type: 'element'
  tag: string
  attrs: Record<string, string>
  children: HtmlNode[]
}

interface HtmlText {
  type: 'text'
  value: string
}

type HtmlNode = HtmlElement | HtmlText

/** Elements that never have a closing tag, so they must not open a nesting level. */
const VOID_ELEMENTS = new Set([
  'area', 'base', 'br', 'col', 'embed', 'hr', 'img', 'input',
  'link', 'meta', 'param', 'source', 'track', 'wbr',
])

/** Elements that belong inside a paragraph rather than starting a block of their own. */
const INLINE_ELEMENTS = new Set([
  'a', 'abbr', 'b', 'bdi', 'bdo', 'br', 'cite', 'code', 'data', 'del', 'dfn',
  'em', 'i', 'img', 'ins', 'kbd', 'mark', 'q', 's', 'samp', 'small', 'span',
  'strike', 'strong', 'sub', 'sup', 'time', 'u', 'var',
])

const NAMED_ENTITIES: Record<string, string> = {
  amp: '&',
  lt: '<',
  gt: '>',
  quot: '"',
  apos: "'",
  nbsp: ' ',
  hellip: '…',
  mdash: '—',
  ndash: '–',
  copy: '©',
  reg: '®',
  trade: '™',
}

function decodeEntities(text: string): string {
  if (!text.includes('&')) return text
  return text.replace(/&(#[Xx][0-9A-Fa-f]+|#[0-9]+|[A-Za-z][A-Za-z0-9]*);/g, (whole, body: string) => {
    if (body[0] === '#') {
      const code =
        body[1] === 'x' || body[1] === 'X'
          ? Number.parseInt(body.slice(2), 16)
          : Number.parseInt(body.slice(1), 10)
      if (!Number.isFinite(code) || code <= 0 || code > 0x10ffff) return whole
      return String.fromCodePoint(code)
    }
    const named = NAMED_ENTITIES[body.toLowerCase()]
    return named === undefined ? whole : named
  })
}

/**
 * Find the `>` that closes the tag opening at `start`, ignoring any that sits
 * inside a quoted attribute value -- a link title or an image alt text is free
 * to contain one.
 */
function findTagEnd(html: string, start: number): number {
  let quote = ''
  for (let i = start + 1; i < html.length; i += 1) {
    const char = html[i]
    if (quote) {
      if (char === quote) quote = ''
    } else if (char === '"' || char === "'") {
      quote = char
    } else if (char === '>') {
      return i
    }
  }
  return -1
}

function parseOpenTag(raw: string): { element: HtmlElement; selfClosing: boolean } {
  const selfClosing = raw.endsWith('/')
  const body = selfClosing ? raw.slice(0, -1) : raw
  const name = /^([A-Za-z][A-Za-z0-9-]*)/.exec(body)
  const attrs: Record<string, string> = {}
  if (name) {
    const attribute = /([A-Za-z_:][-A-Za-z0-9_:.]*)(?:\s*=\s*("[^"]*"|'[^']*'|[^\s"'>]+))?/g
    attribute.lastIndex = name[0].length
    let match = attribute.exec(body)
    while (match) {
      const raw_value = match[2]
      const unquoted =
        raw_value && (raw_value[0] === '"' || raw_value[0] === "'")
          ? raw_value.slice(1, -1)
          : raw_value
      attrs[match[1].toLowerCase()] = unquoted ? decodeEntities(unquoted) : ''
      match = attribute.exec(body)
    }
  }
  return {
    element: { type: 'element', tag: name ? name[1].toLowerCase() : '', attrs, children: [] },
    selfClosing,
  }
}

function parseHtml(html: string): HtmlNode[] {
  const root: HtmlElement = { type: 'element', tag: '#root', attrs: {}, children: [] }
  const open: HtmlElement[] = [root]
  const push = (node: HtmlNode) => open[open.length - 1].children.push(node)

  let cursor = 0
  while (cursor < html.length) {
    const next = html.indexOf('<', cursor)
    if (next === -1) {
      push({ type: 'text', value: decodeEntities(html.slice(cursor)) })
      break
    }
    if (next > cursor) push({ type: 'text', value: decodeEntities(html.slice(cursor, next)) })

    if (html.startsWith('<!--', next)) {
      const end = html.indexOf('-->', next + 4)
      cursor = end === -1 ? html.length : end + 3
      continue
    }

    const end = findTagEnd(html, next)
    if (end === -1) {
      // An unterminated `<` is literal text, not a tag.
      push({ type: 'text', value: decodeEntities(html.slice(next)) })
      break
    }
    const raw = html.slice(next + 1, end)
    cursor = end + 1

    if (raw.startsWith('!') || raw.startsWith('?')) continue

    if (raw.startsWith('/')) {
      const tag = raw.slice(1).trim().toLowerCase()
      for (let depth = open.length - 1; depth > 0; depth -= 1) {
        if (open[depth].tag === tag) {
          open.length = depth
          break
        }
      }
      continue
    }

    const { element, selfClosing } = parseOpenTag(raw)
    push(element)
    if (!selfClosing && !VOID_ELEMENTS.has(element.tag)) open.push(element)
  }

  return root.children
}

function isElement(node: HtmlNode): node is HtmlElement {
  return node.type === 'element'
}

function textContent(node: HtmlNode): string {
  if (node.type === 'text') return node.value
  if (node.tag === 'br') return '\n'
  return node.children.map(textContent).join('')
}

/** A construct the markdown round-trip cannot carry, phrased for the author. */
type ReportLoss = (message: string) => void

interface InlineContext {
  inTableCell: boolean
  report: ReportLoss
}

interface BlockContext {
  report: ReportLoss
  /**
   * True once the blocks being written sit inside a list item or a blockquote.
   * Python-Markdown's `fenced_code` only matches a fence at the outermost
   * level -- indented under a list item or behind a `>` it reads the fence as
   * the first line of an inline code span and folds the whole block into a
   * paragraph. Indented code blocks do nest, so nested code goes out in that
   * form instead.
   */
  codeMustBeIndented: boolean
}

/**
 * Escape the characters that markdown would otherwise read as syntax.
 *
 * `<` and `>` become entities rather than backslash escapes so that text an
 * author typed -- `<script>`, or a shell redirect -- can never be handed back
 * to the renderer as live HTML, and so a line beginning with `>` cannot turn
 * itself into a blockquote. An underscore inside a word (`body_markdown`) is
 * left alone; Python-Markdown does not treat it as emphasis and escaping it
 * would litter the stored markdown and its exports.
 */
function escapeInlineText(raw: string, inTableCell: boolean): string {
  // A newline inside an inline text node is markup, not content -- the renderer
  // puts one after every `<br />` it writes -- and markdown would read it as
  // the end of the line.
  const text = raw.replace(/[\t\r\n]+/g, ' ')
  let out = ''
  for (let i = 0; i < text.length; i += 1) {
    const char = text[i]
    if (char === '&') {
      out += '&amp;'
    } else if (char === '<') {
      out += '&lt;'
    } else if (char === '>') {
      out += '&gt;'
    } else if (char === '\\' || char === '*' || char === '`' || char === '[' || char === ']') {
      out += `\\${char}`
    } else if (char === '|' && inTableCell) {
      out += '\\|'
    } else if (char === '_') {
      const before = text[i - 1]
      const after = text[i + 1]
      const insideWord =
        before !== undefined &&
        after !== undefined &&
        /[0-9A-Za-z]/.test(before) &&
        /[0-9A-Za-z]/.test(after)
      out += insideWord ? '_' : '\\_'
    } else {
      out += char
    }
  }
  return out
}

/**
 * Stop a paragraph whose text happens to start with `#`, `-`, `+` or `1.` from
 * being re-read as a heading or a list item. A digit cannot carry a backslash
 * escape, so for a numbered opener the delimiter is escaped instead.
 */
function escapeLineStart(line: string): string {
  const ordered = /^(\s*)(\d+)([.)])(?=\s|$)/.exec(line)
  if (ordered) {
    return `${ordered[1]}${ordered[2]}\\${ordered[3]}${line.slice(ordered[0].length)}`
  }
  const marker = /^(\s*)([-+#])/.exec(line)
  if (marker) {
    return `${marker[1]}\\${marker[2]}${line.slice(marker[0].length)}`
  }
  return line
}

function escapeBlockText(text: string): string {
  return text
    .replace(/\n[ \t]+/g, '\n')
    .split('\n')
    .map(escapeLineStart)
    .join('\n')
}

/** A link destination containing whitespace or brackets needs the `<...>` form. */
function linkDestination(url: string): string {
  return /[\s()<>]/.test(url) ? `<${url.replace(/[<>]/g, encodeURIComponent)}>` : url
}

function linkTitle(title: string | undefined): string {
  if (!title) return ''
  return ` "${title.replace(/"/g, '\\"')}"`
}

function codeSpan(source: string, report: ReportLoss): string {
  if (source.startsWith('`') || source.endsWith('`')) {
    report('inline code that begins or ends with a backtick, which markdown cannot delimit')
  }
  const runs = source.match(/`+/g)
  const longest = runs ? runs.reduce((widest, run) => Math.max(widest, run.length), 0) : 0
  const fence = '`'.repeat(longest + 1)
  return `${fence}${source}${fence}`
}

/**
 * Emphasis delimiters cannot sit against whitespace, so any space the mark
 * happens to cover is moved outside them.
 */
function wrapMark(inner: string, delimiter: string): string {
  const parts = /^(\s*)([\s\S]*?)(\s*)$/.exec(inner)
  if (!parts || !parts[2]) return inner
  return `${parts[1]}${delimiter}${parts[2]}${delimiter}${parts[3]}`
}

function inlineToMarkdown(nodes: HtmlNode[], ctx: InlineContext): string {
  let out = ''
  for (const node of nodes) {
    if (node.type === 'text') {
      out += escapeInlineText(node.value, ctx.inTableCell)
      continue
    }
    out += inlineElementToMarkdown(node, ctx)
  }
  return out
}

function inlineElementToMarkdown(node: HtmlElement, ctx: InlineContext): string {
  switch (node.tag) {
    case 'br':
      // Two trailing spaces are markdown's hard break; inside a pipe table the
      // cell is one line, so the break has to stay as an element.
      return ctx.inTableCell ? '<br>' : '  \n'
    case 'img': {
      const alt = (node.attrs.alt ?? '').replace(/([[\]])/g, '\\$1')
      return `![${alt}](${linkDestination(node.attrs.src ?? '')}${linkTitle(node.attrs.title)})`
    }
    case 'a': {
      const text = inlineToMarkdown(node.children, ctx)
      const href = node.attrs.href
      if (!href) return text
      return `[${text}](${linkDestination(href)}${linkTitle(node.attrs.title)})`
    }
    case 'code': {
      const source = textContent(node)
      if (source.includes('\n')) {
        ctx.report('a line break inside inline code, which markdown keeps on one line')
      }
      return codeSpan(source.replace(/\n/g, ' '), ctx.report)
    }
    case 'strong':
    case 'b':
      return wrapMark(inlineToMarkdown(node.children, ctx), '**')
    case 'em':
    case 'i':
      return wrapMark(inlineToMarkdown(node.children, ctx), '*')
    case 's':
    case 'strike':
    case 'del': {
      const inner = inlineToMarkdown(node.children, ctx)
      return inner.trim() ? `<s>${inner}</s>` : inner
    }
    case 'u': {
      // StarterKit carries an Underline mark and binds Mod-U to it, so `<u>`
      // reaches here whenever an author uses the shortcut -- the toolbar having
      // no button for it is no protection. Neither markdown nor this renderer
      // has an underline syntax, so it takes the same route as `<s>`: the
      // element goes out as itself, Python-Markdown passes it through and
      // TipTap's Underline parses it back.
      const inner = inlineToMarkdown(node.children, ctx)
      return inner.trim() ? `<u>${inner}</u>` : inner
    }
    default:
      return inlineToMarkdown(node.children, ctx)
  }
}

function describeBlock(tag: string): string {
  if (/^h[1-6]$/.test(tag)) return 'heading'
  switch (tag) {
    case 'ul':
    case 'ol':
      return 'list'
    case 'pre':
      return 'code block'
    case 'blockquote':
      return 'quote'
    case 'table':
      return 'table'
    case 'hr':
      return 'horizontal rule'
    default:
      return tag
  }
}

function tableRows(table: HtmlElement): HtmlElement[] {
  const rows: HtmlElement[] = []
  const visit = (nodes: HtmlNode[]) => {
    for (const node of nodes) {
      if (!isElement(node)) continue
      if (node.tag === 'tr') rows.push(node)
      else if (node.tag === 'thead' || node.tag === 'tbody' || node.tag === 'tfoot') {
        visit(node.children)
      }
    }
  }
  visit(table.children)
  return rows
}

function cellToMarkdown(cell: HtmlElement, report: ReportLoss): string {
  const colspan = Number.parseInt(cell.attrs.colspan ?? '1', 10)
  const rowspan = Number.parseInt(cell.attrs.rowspan ?? '1', 10)
  if (colspan > 1 || rowspan > 1) {
    report('a merged table cell, which a markdown table cannot span across rows or columns')
  }

  const blocks = cell.children.filter(
    (child): child is HtmlElement => isElement(child) && !INLINE_ELEMENTS.has(child.tag)
  )
  const foreign = blocks.find((block) => block.tag !== 'p')
  if (foreign) {
    report(`a ${describeBlock(foreign.tag)} inside a table cell, where markdown allows only one line of text`)
  } else if (blocks.length > 1) {
    report('a table cell holding more than one paragraph, where markdown allows only one line')
  }

  const ctx: InlineContext = { inTableCell: true, report }
  const pieces: string[] = []
  let pending: HtmlNode[] = []
  const flush = () => {
    if (!pending.length) return
    pieces.push(inlineToMarkdown(pending, ctx))
    pending = []
  }
  for (const child of cell.children) {
    if (child.type === 'text' || INLINE_ELEMENTS.has(child.tag)) {
      pending.push(child)
      continue
    }
    flush()
    pieces.push(inlineToMarkdown(child.children, ctx))
  }
  flush()

  return pieces
    .map((piece) => piece.replace(/\s*\n\s*/g, ' ').trim())
    .filter((piece) => piece.length > 0)
    .join('<br>')
}

function tableToMarkdown(table: HtmlElement, report: ReportLoss): string | null {
  const rows = tableRows(table)
  const grid = rows.map((row) =>
    row.children.filter((cell): cell is HtmlElement => isElement(cell) && (cell.tag === 'th' || cell.tag === 'td'))
  )
  const width = grid.reduce((widest, row) => Math.max(widest, row.length), 0)
  if (width === 0) return null
  if (grid.length === 1) {
    // A pipe table is a header plus a body, and a header on its own comes back
    // with an empty row underneath it rather than as the one row it was.
    report('a table with a single row, which markdown reads back with an empty row beneath it')
  }

  const lines = grid.map((row) => {
    const cells: string[] = []
    for (let column = 0; column < width; column += 1) {
      cells.push(row[column] ? cellToMarkdown(row[column], report) : '')
    }
    return `| ${cells.join(' | ')} |`
  })

  // Python-Markdown's `tables` extension reads the first line as the header and
  // requires the delimiter beneath it, so a table that TipTap built without a
  // header row still has to present one.
  const divider = `| ${new Array(width).fill('---').join(' | ')} |`
  return [lines[0], divider, ...lines.slice(1)].join('\n')
}

function codeBlockToMarkdown(pre: HtmlElement, ctx: BlockContext): string {
  const code = pre.children.find((child): child is HtmlElement => isElement(child) && child.tag === 'code')
  const source = textContent(code ?? pre).replace(/\n$/, '')
  const language = /(?:^|\s)(?:language|lang)-([^\s]+)/.exec(code?.attrs.class ?? '')?.[1] ?? ''

  if (ctx.codeMustBeIndented) {
    // The indented form carries the code itself but has nowhere to put the
    // language, so a nested block comes back without its highlighting. That is
    // a loss of presentation, not of the author's text, which is why it does
    // not block the save the way a dropped table cell does.
    return source
      .split('\n')
      .map((line) => `    ${line}`)
      .join('\n')
  }

  const runs = source.match(/^`{3,}/gm)
  const longest = runs ? runs.reduce((widest, run) => Math.max(widest, run.length), 0) : 0
  const fence = '`'.repeat(Math.max(3, longest + 1))
  return `${fence}${language}\n${source}\n${fence}`
}

function listToMarkdown(list: HtmlElement, ordered: boolean, ctx: BlockContext): string | null {
  if (ordered) {
    const start = list.attrs.start
    if (start && start !== '1') {
      ctx.report(`a numbered list starting at ${start}, which markdown renumbers from 1`)
    }
  }

  const items = list.children.filter((child): child is HtmlElement => isElement(child) && child.tag === 'li')
  if (items.length === 0) return null

  const nested: BlockContext = { ...ctx, codeMustBeIndented: true }
  const rendered = items.map((item, index) => {
    const marker = ordered ? `${index + 1}. ` : '- '
    const body = blocksToMarkdown(item.children, nested)
    // Python-Markdown nests on a four-space indent, so every continuation line
    // of an item -- a second paragraph, a sub-list, a code block -- carries
    // one whatever the marker's own width is. The marker keeps its trailing
    // space when the item opens with something other than text, because a bare
    // `-` on a line of its own is a paragraph rather than a list.
    const lines = body.split('\n')
    const head = lines[0] ? `${marker}${lines[0]}`.replace(/\s+$/, '') : marker
    const tail = lines.slice(1).map((line) => (line ? `    ${line}` : ''))
    return [head, ...tail].join('\n')
  })

  // An item that spans more than one line has to be followed by a blank line,
  // or the next item -- which sits at column zero while the previous item's
  // continuation is indented -- is read as part of the nested block instead of
  // as a sibling.
  const separator = rendered.some((item) => item.includes('\n')) ? '\n\n' : '\n'
  return rendered.join(separator)
}

function blockToMarkdown(node: HtmlElement, ctx: BlockContext): string | null {
  const inline: InlineContext = { inTableCell: false, report: ctx.report }

  if (/^h[1-6]$/.test(node.tag)) {
    const level = Number.parseInt(node.tag.slice(1), 10)
    const text = inlineToMarkdown(node.children, inline).replace(/\s*\n\s*/g, ' ').trim()
    return text ? `${'#'.repeat(level)} ${text}` : null
  }

  switch (node.tag) {
    case 'p': {
      const text = inlineToMarkdown(node.children, inline).trim()
      return text ? escapeBlockText(text) : null
    }
    case 'hr':
      return '---'
    case 'ul':
      return listToMarkdown(node, false, ctx)
    case 'ol':
      return listToMarkdown(node, true, ctx)
    case 'li': {
      // A stray `<li>` outside a list reaches here only from pasted markup.
      const orphan: HtmlElement = { type: 'element', tag: 'ul', attrs: {}, children: [node] }
      return listToMarkdown(orphan, false, ctx)
    }
    case 'blockquote': {
      const inner = blocksToMarkdown(node.children, { ...ctx, codeMustBeIndented: true })
      if (!inner) return null
      return inner
        .split('\n')
        .map((line) => (line ? `> ${line}` : '>'))
        .join('\n')
    }
    case 'pre':
      return codeBlockToMarkdown(node, ctx)
    case 'table':
      return tableToMarkdown(node, ctx.report)
    default:
      return blocksToMarkdown(node.children, ctx) || null
  }
}

function blocksToMarkdown(nodes: HtmlNode[], ctx: BlockContext): string {
  const blocks: string[] = []
  let pending: HtmlNode[] = []

  const flush = () => {
    if (!pending.length) return
    const text = inlineToMarkdown(pending, { inTableCell: false, report: ctx.report }).trim()
    pending = []
    if (text) blocks.push(escapeBlockText(text))
  }

  for (const node of nodes) {
    if (node.type === 'text') {
      // Whitespace between two blocks is formatting, not content; whitespace
      // between two inline runs is a word gap and has to survive.
      if (node.value.trim() || pending.length) pending.push(node)
      continue
    }
    if (INLINE_ELEMENTS.has(node.tag) && node.tag !== 'br') {
      pending.push(node)
      continue
    }
    if (node.tag === 'br') {
      if (pending.length) pending.push(node)
      continue
    }
    flush()
    const block = blockToMarkdown(node, ctx)
    if (block) blocks.push(block)
  }
  flush()

  return blocks.join('\n\n')
}

export interface MarkdownConversion {
  markdown: string
  /**
   * Constructs the author can see in the editor that the stored markdown cannot
   * carry. Non-empty means the save must be refused rather than silently
   * flattening them.
   */
  losses: string[]
}

export function htmlToMarkdown(html: string): MarkdownConversion {
  const losses = new Set<string>()
  const ctx: BlockContext = {
    report: (message) => losses.add(message),
    codeMustBeIndented: false,
  }
  const markdown = blocksToMarkdown(parseHtml(html), ctx)
  return { markdown: markdown.trim(), losses: [...losses] }
}

/**
 * Wrap markdown that has no cached render in escaped paragraphs.
 *
 * Rows written before the API started caching `body_html` still carry markdown
 * only. This used to interpolate that text straight into `<p>` tags, which both
 * scrambled the structure and handed any HTML in the body to the browser as
 * live markup. Showing the source escaped is honest about what is there and
 * cannot execute.
 */
export function markdownAsPlainHtml(markdown: string): string {
  const escape = (text: string) =>
    text.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
  return markdown
    .split(/\n{2,}/)
    .map((paragraph) => `<p>${escape(paragraph).replace(/\n/g, '<br>')}</p>`)
    .join('')
}

function requestFailureMessage(err: unknown, fallback: string): string {
  if (isAxiosError(err)) {
    const detail = err.response?.data?.detail
    if (typeof detail === 'string') return detail
    if (!err.response) return 'The server could not be reached.'
    return err.message
  }
  if (err instanceof Error && err.message) return err.message
  return fallback
}

interface EditorToolbarProps {
  editor: ReturnType<typeof useEditor>
}

function EditorToolbar({ editor }: EditorToolbarProps) {
  // The hooks below must run on every render. `useEditor` returns null on the
  // first render and an instance afterwards, so an early `if (!editor) return`
  // above them changed the hook count from 0 to 3 between renders — a
  // Rules-of-Hooks violation that desynchronises React's hook state. The guard
  // now lives inside each callback and the early return sits below them.
  const setLink = useCallback(() => {
    if (!editor) return
    const previousUrl = editor.getAttributes('link').href
    const url = window.prompt('URL', previousUrl)
    if (url === null) return
    if (url === '') {
      editor.chain().focus().extendMarkRange('link').unsetLink().run()
      return
    }
    editor.chain().focus().extendMarkRange('link').setLink({ href: url }).run()
  }, [editor])

  const addImage = useCallback(() => {
    if (!editor) return
    const url = window.prompt('Image URL')
    if (url) {
      editor.chain().focus().setImage({ src: url }).run()
    }
  }, [editor])

  const addTable = useCallback(() => {
    if (!editor) return
    editor.chain().focus().insertTable({ rows: 3, cols: 3, withHeaderRow: true }).run()
  }, [editor])

  if (!editor) return null

  const buttonClass = (isActive: boolean) =>
    `p-2 rounded hover:bg-gray-100 ${isActive ? 'bg-gray-200 text-primary-600' : 'text-gray-600'}`

  return (
    <div className="flex flex-wrap items-center gap-1 p-2 border-b border-gray-200 bg-gray-50">
      {/* Undo/Redo */}
      <button onClick={() => editor.chain().focus().undo().run()} className={buttonClass(false)} title="Undo">
        <Undo className="h-4 w-4" />
      </button>
      <button onClick={() => editor.chain().focus().redo().run()} className={buttonClass(false)} title="Redo">
        <Redo className="h-4 w-4" />
      </button>
      <div className="w-px h-6 bg-gray-300 mx-1" />

      {/* Headings */}
      <button
        onClick={() => editor.chain().focus().toggleHeading({ level: 1 }).run()}
        className={buttonClass(editor.isActive('heading', { level: 1 }))}
        title="Heading 1"
      >
        <Heading1 className="h-4 w-4" />
      </button>
      <button
        onClick={() => editor.chain().focus().toggleHeading({ level: 2 }).run()}
        className={buttonClass(editor.isActive('heading', { level: 2 }))}
        title="Heading 2"
      >
        <Heading2 className="h-4 w-4" />
      </button>
      <button
        onClick={() => editor.chain().focus().toggleHeading({ level: 3 }).run()}
        className={buttonClass(editor.isActive('heading', { level: 3 }))}
        title="Heading 3"
      >
        <Heading3 className="h-4 w-4" />
      </button>
      <div className="w-px h-6 bg-gray-300 mx-1" />

      {/* Text formatting */}
      <button
        onClick={() => editor.chain().focus().toggleBold().run()}
        className={buttonClass(editor.isActive('bold'))}
        title="Bold"
      >
        <Bold className="h-4 w-4" />
      </button>
      <button
        onClick={() => editor.chain().focus().toggleItalic().run()}
        className={buttonClass(editor.isActive('italic'))}
        title="Italic"
      >
        <Italic className="h-4 w-4" />
      </button>
      <button
        onClick={() => editor.chain().focus().toggleStrike().run()}
        className={buttonClass(editor.isActive('strike'))}
        title="Strikethrough"
      >
        <Strikethrough className="h-4 w-4" />
      </button>
      <button
        onClick={() => editor.chain().focus().toggleCode().run()}
        className={buttonClass(editor.isActive('code'))}
        title="Inline Code"
      >
        <Code className="h-4 w-4" />
      </button>
      <div className="w-px h-6 bg-gray-300 mx-1" />

      {/* Lists */}
      <button
        onClick={() => editor.chain().focus().toggleBulletList().run()}
        className={buttonClass(editor.isActive('bulletList'))}
        title="Bullet List"
      >
        <List className="h-4 w-4" />
      </button>
      <button
        onClick={() => editor.chain().focus().toggleOrderedList().run()}
        className={buttonClass(editor.isActive('orderedList'))}
        title="Ordered List"
      >
        <ListOrdered className="h-4 w-4" />
      </button>
      <div className="w-px h-6 bg-gray-300 mx-1" />

      {/* Blocks */}
      <button
        onClick={() => editor.chain().focus().toggleBlockquote().run()}
        className={buttonClass(editor.isActive('blockquote'))}
        title="Blockquote"
      >
        <Quote className="h-4 w-4" />
      </button>
      <button
        onClick={() => editor.chain().focus().toggleCodeBlock().run()}
        className={buttonClass(editor.isActive('codeBlock'))}
        title="Code Block"
      >
        <Code className="h-4 w-4" />
      </button>
      <button
        onClick={() => editor.chain().focus().setHorizontalRule().run()}
        className={buttonClass(false)}
        title="Horizontal Rule"
      >
        <Minus className="h-4 w-4" />
      </button>
      <div className="w-px h-6 bg-gray-300 mx-1" />

      {/* Media & Tables */}
      <button onClick={setLink} className={buttonClass(editor.isActive('link'))} title="Link">
        <LinkIcon className="h-4 w-4" />
      </button>
      <button onClick={addImage} className={buttonClass(false)} title="Image">
        <ImageIcon className="h-4 w-4" />
      </button>
      <button onClick={addTable} className={buttonClass(false)} title="Table">
        <TableIcon className="h-4 w-4" />
      </button>
    </div>
  )
}

export default function ContentEditor() {
  const { id } = useParams()
  const navigate = useNavigate()
  const isNew = id === 'new'

  const [loading, setLoading] = useState(!isNew)
  const [saving, setSaving] = useState(false)
  const [content, setContent] = useState<Content | null>(null)

  // Form state
  const [title, setTitle] = useState('')
  const [description, setDescription] = useState('')
  const [contentType, setContentType] = useState<ContentType>('custom')
  const [tags, setTags] = useState<string[]>([])
  const [tagInput, setTagInput] = useState('')
  const [organization, setOrganization] = useState('')
  const [isPublished, setIsPublished] = useState(false)
  const [walkthroughData, setWalkthroughData] = useState<Walkthrough | null>(null)

  // Assets
  const [assets, setAssets] = useState<ContentAsset[]>([])
  const [uploading, setUploading] = useState(false)

  // Preview mode
  const [showPreview, setShowPreview] = useState(false)

  // Why the last save did not happen: a refusal this page raised because the
  // markdown round-trip would lose something, or the reason the API gave.
  const [saveError, setSaveError] = useState<string | null>(null)
  const [saveLosses, setSaveLosses] = useState<string[]>([])

  const editor = useEditor({
    extensions: [
      StarterKit.configure({
        codeBlock: false,
      }),
      Placeholder.configure({
        placeholder: 'Start writing your content...',
      }),
      Image,
      Link.configure({
        openOnClick: false,
      }),
      Table.configure({
        resizable: true,
      }),
      TableRow,
      TableCell,
      TableHeader,
      CodeBlockLowlight.configure({
        lowlight,
      }),
    ],
    content: '',
    editorProps: {
      attributes: {
        class: 'prose prose-sm sm:prose max-w-none focus:outline-none min-h-[400px] p-4',
      },
    },
  })

  useEffect(() => {
    if (!isNew && id) {
      loadContent(id)
    }
  }, [id, isNew])

  async function loadContent(contentId: string) {
    setLoading(true)
    try {
      const response = await contentApi.get(contentId)
      const data = response.data
      setContent(data)
      setTitle(data.title)
      setDescription(data.description || '')
      setContentType(data.content_type)
      setTags(data.tags)
      setOrganization(data.organization || '')
      setIsPublished(data.is_published)
      setAssets(data.assets)
      setWalkthroughData(data.walkthrough_data || null)

      // Set editor content from the API's cached render of the stored markdown.
      if (editor && data.body_html) {
        editor.commands.setContent(sanitizeHtml(data.body_html))
      } else if (editor && data.body_markdown) {
        editor.commands.setContent(markdownAsPlainHtml(data.body_markdown))
      }
    } catch (err) {
      console.error('Failed to load content:', err)
      navigate('/content')
    } finally {
      setLoading(false)
    }
  }

  async function handleSave() {
    setSaveError(null)
    setSaveLosses([])

    if (!title.trim()) {
      setSaveError('A title is required before this can be saved.')
      return
    }

    const isWalkthrough = contentType === 'student_guide'
    const conversion = htmlToMarkdown(isWalkthrough ? '' : editor?.getHTML() || '')

    // The body is stored as markdown and re-rendered by the API, so anything
    // markdown cannot express is gone the moment this request succeeds. Refuse
    // rather than report success over content the author would then find
    // flattened.
    if (conversion.losses.length > 0) {
      setSaveLosses(conversion.losses)
      return
    }

    setSaving(true)
    try {
      const markdown = conversion.markdown

      if (isNew) {
        const data: ContentCreate = {
          title,
          description: description || undefined,
          content_type: contentType,
          body_markdown: markdown,
          walkthrough_data: isWalkthrough ? walkthroughData : undefined,
          tags,
          organization: organization || undefined,
        }
        const response = await contentApi.create(data)
        navigate(`/content/${response.data.id}`)
      } else if (id) {
        const data: ContentUpdate = {
          title,
          description: description || undefined,
          content_type: contentType,
          body_markdown: markdown,
          walkthrough_data: isWalkthrough ? walkthroughData : undefined,
          tags,
          organization: organization || undefined,
          is_published: isPublished,
        }
        await contentApi.update(id, data)
        await loadContent(id)
      }
    } catch (err) {
      setSaveError(requestFailureMessage(err, 'The server did not say why the save failed.'))
    } finally {
      setSaving(false)
    }
  }

  function handleAddTag() {
    const tag = tagInput.trim()
    if (tag && !tags.includes(tag)) {
      setTags([...tags, tag])
      setTagInput('')
    }
  }

  function handleRemoveTag(tagToRemove: string) {
    setTags(tags.filter((t) => t !== tagToRemove))
  }

  async function handleFileUpload(e: React.ChangeEvent<HTMLInputElement>) {
    const file = e.target.files?.[0]
    if (!file || !id || isNew) return

    setUploading(true)
    try {
      const response = await contentApi.uploadAsset(id, file)
      setAssets([...assets, response.data])
    } catch (err) {
      console.error('Failed to upload:', err)
      alert('Failed to upload file')
    } finally {
      setUploading(false)
    }
  }

  async function handleDeleteAsset(assetId: string) {
    if (!id || !confirm('Delete this asset?')) return
    try {
      await contentApi.deleteAsset(id, assetId)
      setAssets(assets.filter((a) => a.id !== assetId))
    } catch (err) {
      console.error('Failed to delete asset:', err)
    }
  }

  async function handleTogglePublish() {
    if (!id || isNew) return
    try {
      if (isPublished) {
        await contentApi.unpublish(id)
        setIsPublished(false)
      } else {
        await contentApi.publish(id)
        setIsPublished(true)
      }
    } catch (err) {
      console.error('Failed to toggle publish:', err)
    }
  }

  // Get sanitized HTML for preview
  const previewHtml = sanitizeHtml(editor?.getHTML() || '')

  if (loading) {
    return (
      <div className="flex justify-center py-12">
        <div className="animate-spin rounded-full h-8 w-8 border-b-2 border-primary-600"></div>
      </div>
    )
  }

  return (
    <div className="max-w-6xl mx-auto space-y-6">
      {/* Header */}
      <div className="flex items-center justify-between">
        <div className="flex items-center space-x-4">
          <button
            onClick={() => navigate('/content')}
            className="p-2 rounded-md hover:bg-gray-100"
          >
            <ArrowLeft className="h-5 w-5 text-gray-600" />
          </button>
          <h1 className="text-2xl font-semibold text-gray-900">
            {isNew ? 'Create Content' : 'Edit Content'}
          </h1>
        </div>
        <div className="flex items-center space-x-3">
          {!isNew && (
            <button
              onClick={handleTogglePublish}
              className={`inline-flex items-center px-3 py-2 border rounded-md text-sm font-medium ${
                isPublished
                  ? 'border-yellow-300 bg-yellow-50 text-yellow-700 hover:bg-yellow-100'
                  : 'border-green-300 bg-green-50 text-green-700 hover:bg-green-100'
              }`}
            >
              {isPublished ? (
                <>
                  <EyeOff className="h-4 w-4 mr-2" />
                  Unpublish
                </>
              ) : (
                <>
                  <Eye className="h-4 w-4 mr-2" />
                  Publish
                </>
              )}
            </button>
          )}
          {contentType !== 'student_guide' && (
            <button
              onClick={() => setShowPreview(!showPreview)}
              className={`inline-flex items-center px-3 py-2 border rounded-md text-sm font-medium ${
                showPreview
                  ? 'border-primary-500 bg-primary-50 text-primary-700'
                  : 'border-gray-300 bg-white text-gray-700 hover:bg-gray-50'
              }`}
            >
              <Eye className="h-4 w-4 mr-2" />
              Preview
            </button>
          )}
          <button
            onClick={handleSave}
            disabled={saving}
            className="inline-flex items-center px-4 py-2 border border-transparent shadow-sm text-sm font-medium rounded-md text-white bg-primary-600 hover:bg-primary-700 disabled:opacity-50"
          >
            <Save className="h-4 w-4 mr-2" />
            {saving ? 'Saving...' : 'Save'}
          </button>
        </div>
      </div>

      {saveLosses.length > 0 && (
        <div className="rounded-md border border-amber-300 bg-amber-50 p-4">
          <div className="flex">
            <AlertTriangle className="h-5 w-5 text-amber-600 flex-shrink-0" />
            <div className="ml-3">
              <h3 className="text-sm font-medium text-amber-900">Not saved</h3>
              <p className="mt-1 text-sm text-amber-800">
                This content is stored as markdown, which cannot hold the following. Saving
                would drop them without warning, so nothing was sent.
              </p>
              <ul className="mt-2 list-disc pl-5 text-sm text-amber-800 space-y-1">
                {saveLosses.map((loss) => (
                  <li key={loss}>{loss}</li>
                ))}
              </ul>
              <button
                onClick={() => setSaveLosses([])}
                className="mt-3 text-sm font-medium text-amber-900 underline"
              >
                Dismiss
              </button>
            </div>
          </div>
        </div>
      )}

      {saveError && (
        <div className="rounded-md border border-red-300 bg-red-50 p-4">
          <div className="flex">
            <AlertTriangle className="h-5 w-5 text-red-600 flex-shrink-0" />
            <div className="ml-3">
              <h3 className="text-sm font-medium text-red-900">Not saved</h3>
              <p className="mt-1 text-sm text-red-800">{saveError}</p>
              <button
                onClick={() => setSaveError(null)}
                className="mt-3 text-sm font-medium text-red-900 underline"
              >
                Dismiss
              </button>
            </div>
          </div>
        </div>
      )}

      <div className="grid grid-cols-1 lg:grid-cols-3 gap-6">
        {/* Main Editor */}
        <div className="lg:col-span-2 space-y-4">
          {/* Title */}
          <div className="bg-white shadow rounded-lg p-4">
            <input
              type="text"
              value={title}
              onChange={(e) => setTitle(e.target.value)}
              placeholder="Content Title"
              className="w-full text-2xl font-semibold border-none focus:ring-0 p-0 placeholder-gray-400"
            />
            <input
              type="text"
              value={description}
              onChange={(e) => setDescription(e.target.value)}
              placeholder="Short description (optional)"
              className="w-full mt-2 text-sm text-gray-500 border-none focus:ring-0 p-0 placeholder-gray-400"
            />
          </div>

          {/* Editor */}
          {contentType === 'student_guide' ? (
            <div className="bg-white shadow rounded-lg">
              <WalkthroughEditor
                value={walkthroughData}
                onChange={setWalkthroughData}
              />
            </div>
          ) : (
            <div className="bg-white shadow rounded-lg overflow-hidden">
              {showPreview ? (
                <div className="p-6">
                  <div
                    className="prose prose-sm sm:prose max-w-none"
                    dangerouslySetInnerHTML={{ __html: previewHtml }}
                  />
                </div>
              ) : (
                <>
                  <EditorToolbar editor={editor} />
                  <EditorContent editor={editor} />
                </>
              )}
            </div>
          )}
        </div>

        {/* Sidebar */}
        <div className="space-y-4">
          {/* Metadata */}
          <div className="bg-white shadow rounded-lg p-4">
            <h3 className="text-sm font-medium text-gray-900 mb-4">Settings</h3>

            <div className="space-y-4">
              <div>
                <label className="block text-sm font-medium text-gray-700 mb-1">
                  Content Type
                </label>
                <select
                  value={contentType}
                  onChange={(e) => setContentType(e.target.value as ContentType)}
                  className="w-full border border-gray-300 rounded-md py-2 px-3 text-sm"
                >
                  {CONTENT_TYPES.map((type) => (
                    <option key={type.value} value={type.value}>
                      {type.label}
                    </option>
                  ))}
                </select>
              </div>

              <div>
                <label className="block text-sm font-medium text-gray-700 mb-1">
                  Organization
                </label>
                <input
                  type="text"
                  value={organization}
                  onChange={(e) => setOrganization(e.target.value)}
                  placeholder="e.g., Training Command, Cyber Operations"
                  className="w-full border border-gray-300 rounded-md py-2 px-3 text-sm"
                />
              </div>

              <div>
                <label className="block text-sm font-medium text-gray-700 mb-1">
                  Tags
                </label>
                <div className="flex gap-2 mb-2 flex-wrap">
                  {tags.map((tag) => (
                    <span
                      key={tag}
                      className="inline-flex items-center px-2 py-1 rounded text-xs bg-gray-100 text-gray-700"
                    >
                      {tag}
                      <button
                        onClick={() => handleRemoveTag(tag)}
                        className="ml-1 hover:text-red-500"
                      >
                        <X className="h-3 w-3" />
                      </button>
                    </span>
                  ))}
                </div>
                <div className="flex gap-2">
                  <input
                    type="text"
                    value={tagInput}
                    onChange={(e) => setTagInput(e.target.value)}
                    onKeyPress={(e) => e.key === 'Enter' && (e.preventDefault(), handleAddTag())}
                    placeholder="Add tag..."
                    className="flex-1 border border-gray-300 rounded-md py-1 px-2 text-sm"
                  />
                  <button
                    onClick={handleAddTag}
                    className="px-3 py-1 border border-gray-300 rounded-md text-sm hover:bg-gray-50"
                  >
                    <Tag className="h-4 w-4" />
                  </button>
                </div>
              </div>

              {content && (
                <div className="pt-4 border-t border-gray-200 text-xs text-gray-500">
                  <div>Version: {content.version}</div>
                  <div>Created: {new Date(content.created_at).toLocaleDateString()}</div>
                  <div>Updated: {new Date(content.updated_at).toLocaleDateString()}</div>
                </div>
              )}
            </div>
          </div>

          {/* Assets */}
          {!isNew && (
            <div className="bg-white shadow rounded-lg p-4">
              <h3 className="text-sm font-medium text-gray-900 mb-4">Assets</h3>

              <label className="block w-full mb-4">
                <span className="sr-only">Upload file</span>
                <div className="flex items-center justify-center w-full h-20 border-2 border-dashed border-gray-300 rounded-lg hover:border-primary-400 cursor-pointer transition-colors">
                  <div className="text-center">
                    <Upload className="mx-auto h-6 w-6 text-gray-400" />
                    <span className="mt-1 block text-xs text-gray-500">
                      {uploading ? 'Uploading...' : 'Upload image'}
                    </span>
                  </div>
                </div>
                <input
                  type="file"
                  accept="image/*"
                  onChange={handleFileUpload}
                  disabled={uploading}
                  className="hidden"
                />
              </label>

              {assets.length > 0 && (
                <div className="space-y-2">
                  {assets.map((asset) => (
                    <div
                      key={asset.id}
                      className="flex items-center justify-between p-2 bg-gray-50 rounded text-xs"
                    >
                      <div className="flex items-center truncate">
                        <ImageIcon className="h-4 w-4 text-gray-400 mr-2 flex-shrink-0" />
                        <span className="truncate">{asset.filename}</span>
                      </div>
                      <div className="flex items-center space-x-1 ml-2">
                        <button
                          onClick={() => {
                            navigator.clipboard.writeText(asset.file_path)
                          }}
                          className="p-1 hover:bg-gray-200 rounded"
                          title="Copy path"
                        >
                          <Copy className="h-3 w-3" />
                        </button>
                        <button
                          onClick={() => handleDeleteAsset(asset.id)}
                          className="p-1 hover:bg-red-100 text-red-500 rounded"
                          title="Delete"
                        >
                          <X className="h-3 w-3" />
                        </button>
                      </div>
                    </div>
                  ))}
                </div>
              )}
            </div>
          )}
        </div>
      </div>
    </div>
  )
}

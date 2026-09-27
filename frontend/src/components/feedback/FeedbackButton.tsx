/**
 * The way in. Sits beside the notification bell, so it is in the same place on every page.
 *
 * It opens a dialog rather than navigating: a learner is usually inside a split view with a
 * live console, and going to a route loses their place and often the thing they are reporting.
 */
import { useState } from 'react'
import { MessageSquarePlus } from 'lucide-react'

import { FeedbackDialog, type FeedbackTarget } from './FeedbackDialog'

export function FeedbackButton({
  target,
  className = '',
}: {
  target?: FeedbackTarget
  className?: string
}) {
  const [open, setOpen] = useState(false)
  return (
    <>
      <button
        type="button"
        onClick={() => setOpen(true)}
        aria-label="Tell us something"
        title="Tell us something"
        className={`p-2 rounded-md text-gray-400 hover:text-white hover:bg-gray-700 focus:outline-none focus:ring-2 focus:ring-primary-500 ${className}`}
      >
        <MessageSquarePlus className="h-5 w-5" />
      </button>
      <FeedbackDialog isOpen={open} onClose={() => setOpen(false)} target={target} />
    </>
  )
}

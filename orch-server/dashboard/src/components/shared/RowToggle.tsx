import type { MouseEvent, ReactNode } from 'react';
import { Icon } from '@/components/shared/Icon';

interface RowToggleProps {
  open: boolean;
  onToggle: () => void;
  children: ReactNode;
}

// A pressable row whose text is document content: dragging selects the text
// without toggling, and a double press does not select a word.
export function RowToggle({ open, onToggle, children }: RowToggleProps) {
  const handleMouseDown = (e: MouseEvent) => {
    if (e.detail > 1) e.preventDefault();
  };
  const handleClick = () => {
    if (window.getSelection()?.toString()) return;
    onToggle();
  };
  return (
    <button
      type="button"
      className="r-toggle"
      aria-expanded={open}
      onMouseDown={handleMouseDown}
      onClick={handleClick}
    >
      <Icon name="chev" small className="r-chev" />
      <span className="r-text">{children}</span>
    </button>
  );
}

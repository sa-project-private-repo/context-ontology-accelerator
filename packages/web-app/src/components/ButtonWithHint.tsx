// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import { useRef, useState } from "react";
import type { ReactNode } from "react";
import Button from "@cloudscape-design/components/button";
import type { ButtonProps } from "@cloudscape-design/components/button";
import Tooltip from "@cloudscape-design/components/tooltip";

export type ButtonWithHintProps = ButtonProps & { hint: ReactNode };

/**
 * A Button that reveals an explanatory hint on hover/focus, via a Cloudscape
 * Tooltip anchored to the button. Keeps the explanation off the button's own
 * accessible name and avoids a separate info icon next to actionable controls.
 *
 * Extracted from the scan flow (SourceDetail) so the induction flow can use the
 * same idiom for its action buttons (Save changes, Validate, Accept, etc.).
 *
 * A button that is off with a `disabledReason` shows that reason (the Button's
 * own tooltip) instead of the hint, so the two never show at once.
 */
export function ButtonWithHint({ hint, ...buttonProps }: ButtonWithHintProps) {
  const [showHint, setShowHint] = useState(false);
  const wrapperRef = useRef<HTMLSpanElement>(null);
  const saysWhyOff = !!buttonProps.disabled && !!buttonProps.disabledReason;
  return (
    <span
      ref={wrapperRef}
      onMouseEnter={() => setShowHint(true)}
      onMouseLeave={() => setShowHint(false)}
      onFocus={() => setShowHint(true)}
      onBlur={() => setShowHint(false)}
    >
      <Button {...buttonProps} />
      {showHint && !saysWhyOff && (
        <Tooltip
          content={hint}
          position="bottom"
          getTrack={() => wrapperRef.current}
        />
      )}
    </span>
  );
}

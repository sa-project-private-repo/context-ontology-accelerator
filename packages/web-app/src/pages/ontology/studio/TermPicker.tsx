// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

/**
 * An Autosuggest over the draft's own terms (``GET .../studio/terms``).
 *
 * The engine's search reads every term of the draft, so the box waits for a
 * pause in typing before asking, and an answer for text the user has since
 * changed is thrown away. The search is driven from `onChange`, not from
 * Cloudscape's `onLoadItems`: that one is delayed by a fixed 200 ms of its
 * own, which would add to ours, and its timer outlives the component.
 */
import { useCallback, useEffect, useRef, useState } from "react";
import Autosuggest, {
  type AutosuggestProps,
} from "@cloudscape-design/components/autosuggest";
import { useAppI18n } from "../../../i18n/LocaleProvider";
import type { StudioTermHit, StudioTermKind } from "../../../services/studio";

/** How long typing must pause before the engine is asked. */
export const TERM_SEARCH_DEBOUNCE_MS = 275;

export interface TermPickerProps {
  ariaLabel: string;
  placeholder?: string;
  kind?: StudioTermKind;
  disabled?: boolean;
  search: (q: string, kind?: StudioTermKind) => Promise<StudioTermHit[]>;
  onPick: (hit: StudioTermHit) => void;
  /**
   * Every change of the text, so a parent that keeps a pick can drop it once
   * the box no longer shows it. A pick puts the hit's IRI through here first,
   * then calls `onPick`.
   */
  onTextChange?: (text: string) => void;
}

export function TermPicker({
  ariaLabel,
  placeholder,
  kind,
  disabled,
  search,
  onPick,
  onTextChange,
}: TermPickerProps) {
  const { t } = useAppI18n();
  const [text, setText] = useState("");
  const [hits, setHits] = useState<StudioTermHit[]>([]);
  const [status, setStatus] = useState<"finished" | "loading" | "error">(
    "finished",
  );
  const timer = useRef<ReturnType<typeof setTimeout>>();
  // Bumped by every keystroke, pick and unmount: an answer is only used while
  // it still belongs to the latest one.
  const request = useRef(0);

  /** Drops the search that is waiting to start and the one in flight. */
  const cancelPending = useCallback(() => {
    clearTimeout(timer.current);
    request.current += 1;
  }, []);

  useEffect(() => cancelPending, [cancelPending]);

  function load(q: string) {
    cancelPending();
    const mine = request.current;
    if (!q.trim()) {
      setHits([]);
      setStatus("finished");
      return;
    }
    setStatus("loading");
    timer.current = setTimeout(async () => {
      try {
        const found = await search(q, kind);
        if (mine !== request.current) return;
        setHits(found);
        setStatus("finished");
      } catch {
        if (mine !== request.current) return;
        setHits([]);
        setStatus("error");
      }
    }, TERM_SEARCH_DEBOUNCE_MS);
  }

  const options: AutosuggestProps.Option[] = hits.map((h) => ({
    value: h.iri,
    label: h.label,
    description: h.iri,
  }));

  return (
    <Autosuggest
      ariaLabel={ariaLabel}
      value={text}
      disabled={disabled}
      onChange={({ detail }) => {
        setText(detail.value);
        load(detail.value);
        onTextChange?.(detail.value);
      }}
      onSelect={({ detail }) => {
        // Enter with nothing highlighted selects the typed text, which is no
        // hit: leave the search for it alone.
        const hit = hits.find((h) => h.iri === detail.value);
        if (!hit) return;
        // Picking a hit reports it through onChange first, with its IRI as the
        // value; that is not a query. Cancel the search it scheduled.
        cancelPending();
        setStatus("finished");
        setText(hit.label);
        onPick(hit);
      }}
      options={options}
      statusType={status}
      placeholder={placeholder ?? t("Type to search…")}
      empty={t("No matches")}
      loadingText={t("Searching…")}
      errorText={t("Search failed. Edit the text to try again.")}
      filteringType="manual"
      enteredTextLabel={(v) => t('Use: "{value}"', { value: v })}
      hideEnteredTextOption
    />
  );
}

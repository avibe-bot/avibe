import * as React from "react"
import { Check, ChevronsUpDown, Folder, Plus } from "lucide-react"

import { cn } from "../../lib/utils"
import { fieldBaseClass } from "./field"
import { Button } from "./button"
import {
  Command,
  CommandEmpty,
  CommandGroup,
  CommandInput,
  CommandItem,
  CommandList,
} from "./command"
import {
  Popover,
  PopoverContent,
  PopoverTrigger,
} from "./popover"

export interface ComboboxOption {
  value: string
  label: string
}

interface ComboboxProps {
  options: ComboboxOption[]
  value: string
  onValueChange: (value: string) => void
  placeholder?: string
  searchPlaceholder?: string
  emptyText?: string
  allowCustomValue?: boolean
  className?: string
  commitOnClose?: boolean
  createLabel?: (value: string) => string
  createHeading?: string
  /** Show a folder icon before each option + the create row (design.pen group picker). */
  withFolderIcon?: boolean
  /** When set, the create affordance renders as a bordered input + this-labelled button
   *  (design.pen `e3rPI`) instead of an inline command item. */
  createButtonLabel?: string
}

export function Combobox({
  options,
  value,
  onValueChange,
  placeholder = "Select...",
  searchPlaceholder = "Search...",
  emptyText = "No results found.",
  allowCustomValue = true,
  className,
  commitOnClose = false,
  createLabel,
  createHeading,
  withFolderIcon = false,
  createButtonLabel,
}: ComboboxProps) {
  const [open, setOpen] = React.useState(false)
  const [inputValue, setInputValue] = React.useState("")

  const selectedOption = options.find((opt) => opt.value === value)
  const displayValue = selectedOption?.label || value || ""

  // Filter options based on input
  const filteredOptions = React.useMemo(() => {
    if (!inputValue) return options
    const lower = inputValue.toLowerCase()
    return options.filter(
      (opt) =>
        opt.label.toLowerCase().includes(lower) ||
        opt.value.toLowerCase().includes(lower)
    )
  }, [options, inputValue])

  // Check if current input matches any option
  const inputMatchesOption = options.some(
    (opt) => opt.value.toLowerCase() === inputValue.toLowerCase() || opt.label.toLowerCase() === inputValue.toLowerCase()
  )

  const commitCreate = (next: string) => {
    onValueChange(next)
    setOpen(false)
    setInputValue("")
  }

  // Bordered "+ <typed value>  Create" row (design.pen `e3rPI`). Used when the consumer
  // opts in via `createButtonLabel`; otherwise the legacy inline command item is used.
  const renderCreateButtonRow = (typed: string) => (
    <div className="flex items-center gap-2 p-2">
      <span
        className={cn(
          fieldBaseClass,
          "flex h-9 flex-1 items-center gap-2 border-mint/50 px-2.5",
        )}
      >
        <Plus className="size-3.5 shrink-0 text-mint" />
        <span className="truncate text-sm text-foreground">{typed}</span>
      </span>
      <Button type="button" size="sm" onClick={() => commitCreate(typed)}>
        {createButtonLabel}
      </Button>
    </div>
  )

  return (
    <Popover
      open={open}
      onOpenChange={(next) => {
        // Only opt-in consumers (commitOnClose) commit a typed custom value on
        // close; default keeps every other combobox's behavior unchanged.
        if (!next && commitOnClose) {
          if (allowCustomValue && inputValue && inputValue !== value) onValueChange(inputValue)
          setInputValue("")
        }
        setOpen(next)
      }}
    >
      <PopoverTrigger asChild>
        <button
          type="button"
          role="combobox"
          aria-expanded={open}
          className={cn(
            fieldBaseClass,
            "flex h-9 items-center justify-between px-3",
            className
          )}
        >
          <span className={cn("flex items-center gap-2 truncate", !displayValue && "text-muted")}>
            {withFolderIcon && displayValue && <Folder className="size-4 shrink-0 text-muted" />}
            {displayValue || placeholder}
          </span>
          <ChevronsUpDown className="ml-2 h-4 w-4 shrink-0 opacity-50" />
        </button>
      </PopoverTrigger>
      <PopoverContent className="w-[--radix-popover-trigger-width] p-0" align="start">
        <Command shouldFilter={false}>
          <CommandInput
            placeholder={searchPlaceholder}
            value={inputValue}
            onValueChange={setInputValue}
          />
          <CommandList>
            {filteredOptions.length === 0 && !allowCustomValue && (
              <CommandEmpty>{emptyText}</CommandEmpty>
            )}
            {filteredOptions.length === 0 && allowCustomValue && inputValue && (
              createButtonLabel ? (
                renderCreateButtonRow(inputValue)
              ) : (
                <CommandGroup>
                  <CommandItem
                    value={inputValue}
                    onSelect={() => commitCreate(inputValue)}
                  >
                    <Check
                      className={cn(
                        "mr-2 h-4 w-4",
                        value === inputValue ? "opacity-100" : "opacity-0"
                      )}
                    />
                    {createLabel ? createLabel(inputValue) : `Use "${inputValue}"`}
                  </CommandItem>
                </CommandGroup>
              )
            )}
            {filteredOptions.length > 0 && (
              <CommandGroup>
                {filteredOptions.map((option) => {
                  const active = value === option.value
                  return (
                    <CommandItem
                      key={option.value}
                      value={option.value}
                      onSelect={() => commitCreate(option.value)}
                      className={cn(active && withFolderIcon && "bg-mint-soft text-mint data-[selected=true]:bg-mint-soft")}
                    >
                      {withFolderIcon ? (
                        <>
                          <Folder className={cn("mr-2 h-4 w-4 shrink-0", active ? "text-mint" : "text-muted")} />
                          <span className={cn("flex-1 truncate", active ? "font-semibold text-foreground" : "text-foreground")}>
                            {option.label}
                          </span>
                          <Check className={cn("ml-2 h-4 w-4 text-mint", active ? "opacity-100" : "opacity-0")} />
                        </>
                      ) : (
                        <>
                          <Check
                            className={cn("mr-2 h-4 w-4", active ? "opacity-100" : "opacity-0")}
                          />
                          {option.label}
                        </>
                      )}
                    </CommandItem>
                  )
                })}
              </CommandGroup>
            )}
            {/* Show custom value option if input doesn't match existing options */}
            {allowCustomValue && inputValue && !inputMatchesOption && filteredOptions.length > 0 && (
              createButtonLabel ? (
                <>
                  <div className="mx-2 my-1 h-px bg-border" />
                  {renderCreateButtonRow(inputValue)}
                </>
              ) : (
                <CommandGroup heading={createHeading ?? "Custom"}>
                  <CommandItem
                    value={`custom-${inputValue}`}
                    onSelect={() => commitCreate(inputValue)}
                  >
                    <Check
                      className={cn(
                        "mr-2 h-4 w-4",
                        value === inputValue ? "opacity-100" : "opacity-0"
                      )}
                    />
                    {createLabel ? createLabel(inputValue) : `Use "${inputValue}"`}
                  </CommandItem>
                </CommandGroup>
              )
            )}
          </CommandList>
        </Command>
      </PopoverContent>
    </Popover>
  )
}

import * as React from "react"
import { Check, ChevronsUpDown } from "lucide-react"

import { cn } from "../../lib/utils"
import { fieldBaseClass } from "./field"
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

  return (
    <Popover
      open={open}
      onOpenChange={(next) => {
        if (!next) {
          // Commit a typed custom value on close so it isn't lost if the user
          // tabs/clicks away without picking the "Use ..." item.
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
          <span className={cn("truncate", !displayValue && "text-muted")}>
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
              <CommandGroup>
                <CommandItem
                  value={inputValue}
                  onSelect={() => {
                    onValueChange(inputValue)
                    setOpen(false)
                    setInputValue("")
                  }}
                >
                  <Check
                    className={cn(
                      "mr-2 h-4 w-4",
                      value === inputValue ? "opacity-100" : "opacity-0"
                    )}
                  />
                  Use "{inputValue}"
                </CommandItem>
              </CommandGroup>
            )}
            {filteredOptions.length > 0 && (
              <CommandGroup>
                {filteredOptions.map((option) => (
                  <CommandItem
                    key={option.value}
                    value={option.value}
                    onSelect={() => {
                      onValueChange(option.value)
                      setOpen(false)
                      setInputValue("")
                    }}
                  >
                    <Check
                      className={cn(
                        "mr-2 h-4 w-4",
                        value === option.value ? "opacity-100" : "opacity-0"
                      )}
                    />
                    {option.label}
                  </CommandItem>
                ))}
              </CommandGroup>
            )}
            {/* Show custom value option if input doesn't match existing options */}
            {allowCustomValue && inputValue && !inputMatchesOption && filteredOptions.length > 0 && (
              <CommandGroup heading="Custom">
                <CommandItem
                  value={`custom-${inputValue}`}
                  onSelect={() => {
                    onValueChange(inputValue)
                    setOpen(false)
                    setInputValue("")
                  }}
                >
                  <Check
                    className={cn(
                      "mr-2 h-4 w-4",
                      value === inputValue ? "opacity-100" : "opacity-0"
                    )}
                  />
                  Use "{inputValue}"
                </CommandItem>
              </CommandGroup>
            )}
          </CommandList>
        </Command>
      </PopoverContent>
    </Popover>
  )
}

import { useEffect, useMemo, useState, type Key } from "react";
import { Autocomplete, AutocompleteItem } from "@heroui/react";
import type { ModelOption } from "../shared/model-options";
import { uniqueModelOptions } from "../shared/model-options";

type ModelComboboxProps = {
  label: string;
  value: string;
  onValueChange: (value: string) => void;
  options?: ModelOption[];
  placeholder?: string;
  description?: string;
  inputClassNames?: Record<string, string>;
  className?: string;
};

export function ModelCombobox({
  label,
  value,
  onValueChange,
  options,
  placeholder,
  description,
  inputClassNames,
  className,
}: ModelComboboxProps) {
  const items = useMemo(() => uniqueModelOptions(options), [options]);
  const [inputValue, setInputValue] = useState(value || "");

  useEffect(() => {
    setInputValue(value || "");
  }, [value]);

  const displayItems = useMemo(() => {
    const current = String(inputValue || "").trim();
    if (!current) return items;
    const exists = items.some((item) => item.value.toLowerCase() === current.toLowerCase());
    if (exists) return items;
    return [
      {
        value: current,
        label: current,
        description: "自定义模型",
      },
      ...items,
    ];
  }, [inputValue, items]);

  const selectedKey = useMemo(() => {
    const current = String(value || "").trim().toLowerCase();
    if (!current) return null;
    return displayItems.find((item) => item.value.toLowerCase() === current)?.value ?? null;
  }, [displayItems, value]);

  return (
    <Autocomplete
      label={label}
      labelPlacement="outside"
      inputValue={inputValue}
      selectedKey={selectedKey}
      onInputChange={(nextValue) => {
        setInputValue(nextValue);
        onValueChange(nextValue);
      }}
      onSelectionChange={(key: Key | null) => {
        if (key !== null) {
          const nextValue = String(key);
          setInputValue(nextValue);
          onValueChange(nextValue);
        }
      }}
      allowsCustomValue
      allowsEmptyCollection
      menuTrigger="focus"
      placeholder={placeholder || "搜索或直接输入模型名"}
      description={description || "支持搜索候选项，也可以直接输入任意模型名"}
      inputProps={{ classNames: inputClassNames }}
      className={className}
      listboxProps={{ emptyContent: "没有匹配项，继续输入即可作为自定义模型保存" }}
    >
      {displayItems.map((item) => (
        <AutocompleteItem key={item.value} textValue={item.label}>
          <div className="flex min-w-0 flex-col">
            <span className="truncate text-sm">{item.label}</span>
            {item.description ? (
              <span className="truncate text-xs text-default-400">{item.description}</span>
            ) : null}
          </div>
        </AutocompleteItem>
      ))}
    </Autocomplete>
  );
}

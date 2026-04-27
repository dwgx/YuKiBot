import { useMemo, type Key } from "react";
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
  const items = useMemo(() => {
    const current = String(value || "").trim();
    const base = uniqueModelOptions(options);
    if (current && !base.some((item) => item.value.toLowerCase() === current.toLowerCase())) {
      return [
        { value: current, label: current, description: "当前自定义模型" },
        ...base,
      ];
    }
    return base;
  }, [options, value]);

  const selectedKey = value ? value : null;

  return (
    <Autocomplete
      label={label}
      labelPlacement="outside"
      inputValue={value}
      selectedKey={selectedKey}
      onInputChange={onValueChange}
      onSelectionChange={(key: Key | null) => {
        if (key !== null) onValueChange(String(key));
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
      {items.map((item) => (
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

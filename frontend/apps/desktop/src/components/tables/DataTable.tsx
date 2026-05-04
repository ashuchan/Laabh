import React, { useState } from 'react';
import {
  useReactTable,
  getCoreRowModel,
  getSortedRowModel,
  getFilteredRowModel,
  flexRender,
  type ColumnDef,
  type SortingState,
  type RowSelectionState,
} from '@tanstack/react-table';
import { ChevronUp, ChevronDown } from 'lucide-react';
import { cn } from '../../lib/cn';

interface DataTableProps<T> {
  data: T[];
  columns: ColumnDef<T, unknown>[];
  onRowClick?: (row: T) => void;
  getRowId?: (row: T) => string;
  selectedRowId?: string | null;
  globalFilter?: string;
  enableRowSelection?: boolean;
  onRowSelectionChange?: (selection: RowSelectionState) => void;
  className?: string;
  stickyHeader?: boolean;
}

export function DataTable<T>({
  data,
  columns,
  onRowClick,
  getRowId,
  selectedRowId,
  globalFilter,
  enableRowSelection,
  onRowSelectionChange,
  className,
  stickyHeader,
}: DataTableProps<T>) {
  const [sorting, setSorting] = useState<SortingState>([]);
  const [rowSelection, setRowSelection] = useState<RowSelectionState>({});

  const table = useReactTable({
    data,
    columns,
    state: { sorting, globalFilter, rowSelection },
    onSortingChange: setSorting,
    onRowSelectionChange: (updater) => {
      const next = typeof updater === 'function' ? updater(rowSelection) : updater;
      setRowSelection(next);
      onRowSelectionChange?.(next);
    },
    getCoreRowModel: getCoreRowModel(),
    getSortedRowModel: getSortedRowModel(),
    getFilteredRowModel: getFilteredRowModel(),
    enableRowSelection,
    getRowId,
  });

  return (
    <div className={cn('overflow-auto', className)}>
      <table className="w-full border-collapse text-xs">
        <thead className={cn(stickyHeader && 'sticky top-0 z-10')}>
          {table.getHeaderGroups().map((hg) => (
            <tr key={hg.id}>
              {hg.headers.map((header) => (
                <th
                  key={header.id}
                  className={cn(
                    'border-b border-[var(--color-border)] bg-[var(--color-surface)] px-3 py-2 text-left font-semibold text-[var(--color-text-secondary)] uppercase tracking-wider text-[10px] whitespace-nowrap',
                    header.column.getCanSort() && 'cursor-pointer select-none hover:text-[var(--color-text)]',
                  )}
                  onClick={header.column.getToggleSortingHandler()}
                  style={{ width: header.getSize() !== 150 ? header.getSize() : undefined }}
                >
                  <div className="flex items-center gap-1">
                    {flexRender(header.column.columnDef.header, header.getContext())}
                    {header.column.getIsSorted() === 'asc' && <ChevronUp size={10} />}
                    {header.column.getIsSorted() === 'desc' && <ChevronDown size={10} />}
                  </div>
                </th>
              ))}
            </tr>
          ))}
        </thead>
        <tbody>
          {table.getRowModel().rows.map((row) => {
            const rowId = getRowId ? getRowId(row.original) : row.id;
            const isSelected = selectedRowId === rowId || row.getIsSelected();
            return (
              <tr
                key={row.id}
                onClick={() => onRowClick?.(row.original)}
                className={cn(
                  'border-b border-[var(--color-border)] transition-colors',
                  onRowClick && 'cursor-pointer hover:bg-[var(--color-surface-elevated)]',
                  isSelected && 'bg-[var(--color-surface-elevated)]',
                )}
              >
                {row.getVisibleCells().map((cell) => (
                  <td
                    key={cell.id}
                    className="px-3 py-2 text-[var(--color-text)] whitespace-nowrap"
                  >
                    {flexRender(cell.column.columnDef.cell, cell.getContext())}
                  </td>
                ))}
              </tr>
            );
          })}
        </tbody>
      </table>
      {table.getRowModel().rows.length === 0 && (
        <div className="py-8 text-center text-xs text-[var(--color-text-muted)]">No rows</div>
      )}
    </div>
  );
}

"""Bounded page selection, shared by host validation and isolated PDF workers."""
MAX_DOCUMENT_PAGES = 10000
MAX_SELECTED_PAGES = 60


def validate_selection(request):
    pages, ranges = request.get('pages'), request.get('page_ranges')
    if pages is not None and ranges is not None:
        raise ValueError('Use pages or page_ranges, not both')
    if pages is not None:
        if (not isinstance(pages, list) or not 1 <= len(pages) <= MAX_SELECTED_PAGES
                or any(type(p) is not int or p < 1 for p in pages)
                or len(set(pages)) != len(pages)):
            raise ValueError('pages requires 1..60 distinct positive page numbers')
        return sorted(pages), 'explicit_pages'
    if ranges is not None:
        if not isinstance(ranges, list) or not 1 <= len(ranges) <= MAX_SELECTED_PAGES:
            raise ValueError('page_ranges requires bounded start/end ranges')
        result = []
        for item in ranges:
            if (not isinstance(item, dict) or set(item) != {'start', 'end'}
                    or type(item['start']) is not int or type(item['end']) is not int
                    or not 1 <= item['start'] <= item['end']
                    or item['end']-item['start']+1 > MAX_SELECTED_PAGES-len(result)):
                raise ValueError('Ranges must be positive, ordered and select at most 60 pages')
            result.extend(range(item['start'], item['end']+1))
        if len(set(result)) != len(result):
            raise ValueError('Page ranges must not overlap')
        return sorted(result), 'explicit_ranges'
    return None, None


def select_pages(request, total):
    if type(total) is not int or not 1 <= total <= MAX_DOCUMENT_PAGES:
        raise ValueError('PDF total page count must be between 1 and 10000')
    selected, mode = validate_selection(request)
    if selected is None:
        if total <= MAX_SELECTED_PAGES:
            selected, mode = list(range(1, total+1)), 'all_short_document'
        else:
            selected, mode = [1, (total+1)//2, total], 'sampled_long_document'
    if selected[-1] > total:
        raise ValueError('Selected page is outside the actual PDF')
    return {'page_count': total, 'selected_pages': selected, 'selection_mode': mode,
            'complete_document_review': len(selected) == total,
            'selection_limits': {'document_pages': MAX_DOCUMENT_PAGES, 'selected_pages': MAX_SELECTED_PAGES}}

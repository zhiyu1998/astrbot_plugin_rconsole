import re
import os
from typing import List, Dict, Any, Union, Sequence, Optional

import astrbot.api.message_components as Comp
from astrbot.api.event import AstrMessageEvent

def delete_boring_characters(sentence):
    """
        去除标题的特殊字符
    :param sentence:
    :return:
    """
    return re.sub(r'[0-9\'!"∀〃#$%&\'()*+,-./:;<=>?@，。?★、…【】《》？""''！[\\]^_`{|}~～\s]+', "", sentence)

def remove_files(file_paths: List[str]) -> Dict[str, str]:
    """
    根据路径删除文件

    Parameters:
    *file_paths (str): 要删除的一个或多个文件路径

    Returns:
    dict: 一个以文件路径为键、删除状态为值的字典
    """
    results = { }

    for file_path in file_paths:
        if os.path.exists(file_path):
            try:
                os.remove(file_path)
                results[file_path] = 'remove'
            except Exception as e:
                results[file_path] = f'error: {e}'
        else:
            results[file_path] = 'don\'t exist'

    return results

def create_forward_message(content_list: List[Union[List[Any], Dict[str, Any]]], 
                          default_name: Optional[str] = None, 
                          default_uin: Optional[Union[int, str]] = None) -> Comp.Nodes:
    """
    创建合并转发消息
    
    :param content_list: 消息内容列表，每个元素可以是:
                        1. 列表：包含 Plain, Image 等消息组件
                        2. 字典：包含 'uin', 'name', 'content' 的键值对
    :param default_name: 默认显示的发送者名称，如果为None会尝试使用发送者信息
    :param default_uin: 默认显示的发送者ID，如果为None会尝试使用发送者信息
    :return: Nodes对象，包含所有的Node
    
    使用示例:
    >>> nodes = create_forward_message([
    >>>     [Comp.Plain("第一条消息")],
    >>>     [Comp.Image.fromURL("http://example.com/image.jpg")],
    >>>     {"uin": 10001, "name": "小明", "content": [Comp.Plain("自定义发送者")]}
    >>> ])
    >>> yield event.chain_result([nodes])
    """
    # 创建一个Nodes对象
    nodes = Comp.Nodes([])
    
    for item in content_list:
        if isinstance(item, list):
            # 如果是列表，则使用默认的发送者信息
            node = Comp.Node(
                uin=default_uin,
                name=default_name,
                content=item
            )
            nodes.nodes.append(node)
        elif isinstance(item, dict) and 'content' in item:
            # 如果是字典且包含content键，则使用字典中的发送者信息
            node = Comp.Node(
                uin=item.get('uin', default_uin),
                name=item.get('name', default_name),
                content=item['content']
            )
            nodes.nodes.append(node)
    
    return nodes

async def send_forward_message(event: AstrMessageEvent, 
                             content_list: List[Union[List[Any], Dict[str, Any]]],
                             default_name: Optional[str] = None, 
                             default_uin: Optional[Union[int, str]] = None) -> Any:
    """
    发送合并转发消息
    
    :param event: AstrMessageEvent实例
    :param content_list: 消息内容列表
    :param default_name: 默认显示的发送者名称，如果为None将使用发送者的名称
    :param default_uin: 默认显示的发送者ID，如果为None将使用发送者的ID
    :return: 消息发送结果
    
    使用示例:
    >>> # 使用发送者信息
    >>> yield await send_forward_message(event, [
    >>>     [Comp.Plain("第一条消息")],
    >>>     [Comp.Image.fromURL("http://example.com/image.jpg")]
    >>> ])
    >>> 
    >>> # 自定义名称
    >>> yield await send_forward_message(event, [
    >>>     [Comp.Plain("第一条消息")],
    >>>     [Comp.Image.fromURL("http://example.com/image.jpg")]
    >>> ], default_name="自定义名称")
    """
    # 如果未提供默认值，则使用发送者信息
    if default_name is None:
        default_name = event.get_sender_name()
    if default_uin is None:
        default_uin = event.get_sender_id()
        
    nodes = create_forward_message(content_list, default_name, default_uin)
    return event.chain_result([nodes])